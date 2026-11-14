package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Wish is what the app asked for one plugin.
type Wish struct {
	Enabled  bool              `json:"enabled"`
	Settings map[string]string `json:"settings,omitempty"`
}

// Report is how a plugin stands, for the app.
type Report struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Version  string `json:"version"`
	Enabled  bool   `json:"enabled"`
	Running  bool   `json:"running"`
	Port     int    `json:"port"`
	Released bool   `json:"released"`
	// Note is the last thing that went wrong (an update rolled back, a pull that failed); "" when fine.
	Note    string `json:"note,omitempty"`
	Updated string `json:"updated,omitempty"`
}

// Hub keeps the enabled plugins running at the catalog's digests.
type Hub struct {
	mu      sync.Mutex // one reconcile at a time
	data    string
	engine  Engine
	healthy func(port int, path string) bool
	key     string
	url     string

	state   sync.Mutex
	catalog *Catalog
	fetched time.Time
	wishes  map[string]Wish
	notes   map[string]string
	updated map[string]string
	lastErr string
}

func newHub(data string, e Engine, catalogURL, key string) *Hub {
	h := &Hub{data: data, engine: e, healthy: healthy, key: key, url: catalogURL,
		wishes: map[string]Wish{}, notes: map[string]string{}, updated: map[string]string{}}
	if b, err := os.ReadFile(filepath.Join(data, "wishes.json")); err == nil {
		_ = json.Unmarshal(b, &h.wishes)
	}
	// The last catalog that passed its signature: plugins keep running while GitHub is away.
	if c, err := h.load(); err == nil {
		h.catalog = c
	}
	return h
}

func (h *Hub) load() (*Catalog, error) {
	data, err := os.ReadFile(filepath.Join(h.data, "catalog.json"))
	if err != nil {
		return nil, err
	}
	sig, err := os.ReadFile(filepath.Join(h.data, "catalog.json.sig"))
	if err != nil {
		return nil, err
	}
	return verify(data, sig, h.key)
}

// Refresh fetches the catalog; kept only when signed and not older than the one in hand.
func (h *Hub) Refresh() error {
	data, sig, err := fetch(h.url)
	if err != nil {
		return err
	}
	c, err := verify(data, sig, h.key)
	if err != nil {
		return err
	}
	h.state.Lock()
	old := h.catalog
	h.state.Unlock()
	if old != nil && c.Version < old.Version {
		return fmt.Errorf("catalog %d is older than %d, kept the newer", c.Version, old.Version)
	}
	if err := writeFile(filepath.Join(h.data, "catalog.json"), data); err != nil {
		return err
	}
	if err := writeFile(filepath.Join(h.data, "catalog.json.sig"), sig); err != nil {
		return err
	}
	h.state.Lock()
	h.catalog = c
	h.fetched = time.Now()
	h.state.Unlock()
	return nil
}

// Want records the app's wish for a plugin; the next reconcile carries it out.
func (h *Hub) Want(id string, w Wish) error {
	h.state.Lock()
	defer h.state.Unlock()
	if h.catalog == nil || h.catalog.plugin(id) == nil {
		return fmt.Errorf("no plugin %q", id)
	}
	allowed := map[string]bool{}
	for _, s := range h.catalog.plugin(id).Settings {
		allowed[s] = true
	}
	for k := range w.Settings {
		if !allowed[k] {
			return fmt.Errorf("%s has no setting %q", id, k)
		}
	}
	// The app's switch sends on/off only: the settings given before stay (the recorder's
	// Dispatcharr address, 14.11.2026).
	if w.Settings == nil {
		w.Settings = h.wishes[id].Settings
	}
	h.wishes[id] = w
	b, _ := json.MarshalIndent(h.wishes, "", "  ")
	return writeFile(filepath.Join(h.data, "wishes.json"), b)
}

// Reconcile makes the containers match the catalog and the wishes: installs, updates (rolling
// back what fails its health check), stops and removes. Volumes stay.
func (h *Hub) Reconcile() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.state.Lock()
	cat := h.catalog
	wishes := map[string]Wish{}
	for k, v := range h.wishes {
		wishes[k] = v
	}
	h.state.Unlock()
	if cat == nil {
		return
	}
	boxes, err := h.engine.List()
	if err != nil {
		h.setErr("docker: " + err.Error())
		return
	}
	h.setErr("")
	byName := map[string]Box{}
	for _, b := range boxes {
		byName[b.Name] = b
	}
	for _, p := range cat.Plugins {
		name := "opentv-" + p.ID
		cur, has := byName[name]
		// A rollback left half-done (the hub stopped in between).
		if old, ok := byName[name+"-old"]; ok {
			if has {
				_ = h.engine.Remove(old.ID)
			} else if h.engine.Rename(old.ID, name) == nil {
				old.Name = name
				cur, has = old, true
			}
		}
		w := wishes[p.ID]
		if !w.Enabled || p.Digest == "" {
			if has {
				log.Printf("%s: off", p.ID)
				_ = h.engine.Stop(cur.ID)
				if err := h.engine.Remove(cur.ID); err != nil {
					h.note(p.ID, err.Error())
				}
			}
			continue
		}
		if has && cur.Digest == p.Digest {
			if !cur.Running {
				if err := h.engine.Start(cur.ID); err != nil {
					h.note(p.ID, err.Error())
				}
			}
			continue
		}
		h.install(p, w, cur, has)
	}
	// Containers of plugins the catalog no longer has.
	for _, b := range boxes {
		if cat.plugin(b.Plugin) == nil {
			log.Printf("%s: gone from the catalog", b.Plugin)
			_ = h.engine.Remove(b.ID)
		}
	}
}

func (h *Hub) install(p Plugin, w Wish, cur Box, has bool) {
	name := "opentv-" + p.ID
	log.Printf("%s: %s %s", p.ID, map[bool]string{true: "update to", false: "install"}[has], p.Version)
	if err := h.engine.Pull(p.Image, p.Digest); err != nil {
		h.note(p.ID, err.Error())
		return
	}
	if has {
		_ = h.engine.Stop(cur.ID)
		if err := h.engine.Rename(cur.ID, name+"-old"); err != nil {
			h.note(p.ID, err.Error())
			_ = h.engine.Start(cur.ID)
			return
		}
	}
	back := func(why string) {
		log.Printf("%s: %s, back to the version before", p.ID, why)
		if has {
			if h.engine.Rename(cur.ID, name) == nil {
				_ = h.engine.Start(cur.ID)
			}
			h.note(p.ID, "Update zurückgenommen: "+why)
		} else {
			h.note(p.ID, why)
		}
	}
	// A container of that name the hub did not make (Scout set up by hand before the hub, owner,
	// 14.11.2026): it would block the name and the port. It is stopped and kept as "-vorher",
	// for its owner to look at or delete; the hub's own one takes its place.
	if !has {
		if other, err := h.engine.Named(name); err == nil && other != "" {
			aside := name + "-vorher"
			if old, _ := h.engine.Named(aside); old != "" {
				aside = fmt.Sprintf("%s-vorher-%d", name, time.Now().Unix())
			}
			log.Printf("%s: %s was not made by the hub: stopped, kept as %s", p.ID, name, aside)
			_ = h.engine.Stop(other)
			if err := h.engine.Rename(other, aside); err != nil {
				h.note(p.ID, err.Error())
				return
			}
		}
	}
	id, err := h.engine.Create(name, spec(p, w))
	if err != nil {
		back(err.Error())
		return
	}
	if err := h.engine.Start(id); err != nil {
		_ = h.engine.Remove(id)
		back(err.Error())
		return
	}
	if p.Health != "" && !h.healthy(p.Port, p.Health) {
		_ = h.engine.Remove(id)
		back("startet nicht richtig")
		return
	}
	if has {
		_ = h.engine.Remove(cur.ID)
	}
	h.note(p.ID, "")
	h.state.Lock()
	h.updated[p.ID] = time.Now().Format(time.RFC3339)
	h.state.Unlock()
	log.Printf("%s: running %s", p.ID, p.Version)
}

func spec(p Plugin, w Wish) Spec {
	env := map[string]string{}
	for k, v := range p.Env {
		env[k] = v
	}
	for k, v := range w.Settings {
		env[k] = v
	}
	s := Spec{Plugin: p.ID, Image: p.Image, Digest: p.Digest, Port: p.Port}
	keys := make([]string, 0, len(env))
	for k := range env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		s.Env = append(s.Env, k+"="+env[k])
	}
	vols := make([]string, 0, len(p.Volumes))
	for n := range p.Volumes {
		vols = append(vols, n)
	}
	sort.Strings(vols)
	for _, n := range vols {
		s.Binds = append(s.Binds, "opentv-"+p.ID+"-"+n+":"+p.Volumes[n])
	}
	return s
}

// healthy asks the plugin on its port for up to 90 s (the hub runs on the NAS's own network).
func healthy(port int, path string) bool {
	client := &http.Client{Timeout: 5 * time.Second}
	for end := time.Now().Add(90 * time.Second); time.Now().Before(end); time.Sleep(3 * time.Second) {
		r, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d%s", port, path))
		if err == nil {
			r.Body.Close()
			if r.StatusCode == 200 {
				return true
			}
		}
	}
	return false
}

func (h *Hub) note(id, msg string) {
	if msg != "" {
		log.Printf("%s: %s", id, msg)
	}
	h.state.Lock()
	h.notes[id] = msg
	h.state.Unlock()
}

func (h *Hub) setErr(msg string) {
	h.state.Lock()
	h.lastErr = msg
	h.state.Unlock()
}

// Status is everything the app shows.
func (h *Hub) Status() map[string]any {
	boxes, _ := h.engine.List()
	running := map[string]bool{}
	for _, b := range boxes {
		if b.Running && !strings.HasSuffix(b.Name, "-old") {
			running[b.Plugin] = true
		}
	}
	h.state.Lock()
	defer h.state.Unlock()
	out := map[string]any{"app": "opentv-hub", "version": Version, "error": h.lastErr}
	plugins := []Report{}
	if c := h.catalog; c != nil {
		out["catalog"] = map[string]any{"version": c.Version, "published": c.Published, "fetched": h.fetched.Format(time.RFC3339)}
		if c.Hub.Version != "" && c.Hub.Version != Version {
			out["hubUpdate"] = c.Hub.Version
		}
		for _, p := range c.Plugins {
			plugins = append(plugins, Report{ID: p.ID, Name: p.Name, Version: p.Version, Enabled: h.wishes[p.ID].Enabled,
				Running: running[p.ID], Port: p.Port, Released: p.Digest != "", Note: h.notes[p.ID], Updated: h.updated[p.ID]})
		}
	}
	out["plugins"] = plugins
	return out
}

func writeFile(path string, b []byte) error {
	tmp := path + ".part"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
