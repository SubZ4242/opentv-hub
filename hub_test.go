package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// fake is Docker as a map, with a switch to make a new image fail its health check.
type fake struct {
	boxes  map[string]*Box
	n      int
	pulled []string
}

func newFake() *fake { return &fake{boxes: map[string]*Box{}} }

func (f *fake) Pull(image, digest string) error { f.pulled = append(f.pulled, digest); return nil }
func (f *fake) List() ([]Box, error) {
	var out []Box
	for _, b := range f.boxes {
		// As Docker's label filter: only the hub's own.
		if b.Plugin != "" {
			out = append(out, *b)
		}
	}
	return out, nil
}
func (f *fake) Named(name string) (string, error) {
	for _, b := range f.boxes {
		if b.Name == name {
			return b.ID, nil
		}
	}
	return "", nil
}
func (f *fake) Create(name string, s Spec) (string, error) {
	for _, b := range f.boxes {
		if b.Name == name {
			return "", fmt.Errorf("name %s taken", name)
		}
	}
	f.n++
	id := fmt.Sprint("c", f.n)
	f.boxes[id] = &Box{ID: id, Name: name, Plugin: s.Plugin, Digest: s.Digest}
	return id, nil
}
func (f *fake) Start(id string) error  { f.boxes[id].Running = true; return nil }
func (f *fake) Stop(id string) error   { f.boxes[id].Running = false; return nil }
func (f *fake) Remove(id string) error { delete(f.boxes, id); return nil }
func (f *fake) Rename(id, name string) error {
	f.boxes[id].Name = name
	return nil
}

func (f *fake) only(t *testing.T) Box {
	t.Helper()
	if len(f.boxes) != 1 {
		t.Fatalf("want one container, have %d: %+v", len(f.boxes), f.boxes)
	}
	for _, b := range f.boxes {
		return *b
	}
	return Box{}
}

func signed(t *testing.T, priv ed25519.PrivateKey, c Catalog) (data, sig []byte) {
	t.Helper()
	data, _ = json.Marshal(c)
	return data, []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data)))
}

func setup(t *testing.T, digest string) (*Hub, *fake, ed25519.PrivateKey) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	dir := t.TempDir()
	cat := Catalog{Version: 1, Plugins: []Plugin{{ID: "scout", Name: "Entdecken", Image: "ghcr.io/x/scout", Digest: digest, Port: 35120, Health: "/health",
		Volumes: map[string]string{"data": "/app/data"}}}}
	data, sig := signed(t, priv, cat)
	os.WriteFile(filepath.Join(dir, "catalog.json"), data, 0o600)
	os.WriteFile(filepath.Join(dir, "catalog.json.sig"), sig, 0o600)
	f := newFake()
	h := newHub(dir, f, "", base64.StdEncoding.EncodeToString(pub))
	if h.catalog == nil {
		t.Fatal("signed catalog not loaded")
	}
	h.healthy = func(int, string) bool { return true }
	return h, f, priv
}

func (h *Hub) setDigest(d string) { h.catalog.Plugins[0].Digest = d }

func TestInstallUpdateRollbackOff(t *testing.T) {
	h, f, _ := setup(t, "sha256:a")
	h.Reconcile()
	if len(f.boxes) != 0 {
		t.Fatal("installed without being asked")
	}
	if err := h.Want("scout", Wish{Enabled: true}); err != nil {
		t.Fatal(err)
	}
	h.Reconcile()
	b := f.only(t)
	if b.Name != "opentv-scout" || b.Digest != "sha256:a" || !b.Running {
		t.Fatalf("install: %+v", b)
	}

	// An update that comes up.
	h.setDigest("sha256:b")
	h.Reconcile()
	if b = f.only(t); b.Digest != "sha256:b" || !b.Running || b.Name != "opentv-scout" {
		t.Fatalf("update: %+v", b)
	}

	// An update that does not: the one before runs again, under its own name.
	h.setDigest("sha256:c")
	h.healthy = func(int, string) bool { return false }
	h.Reconcile()
	if b = f.only(t); b.Digest != "sha256:b" || !b.Running || b.Name != "opentv-scout" {
		t.Fatalf("rollback: %+v", b)
	}
	if h.Status()["plugins"].([]Report)[0].Note == "" {
		t.Fatal("rollback not reported")
	}

	// Stopped by hand: started again; nothing pulled anew.
	for _, x := range f.boxes {
		x.Running = false
	}
	h.healthy = func(int, string) bool { return true }
	h.setDigest("sha256:b")
	pulls := len(f.pulled)
	h.Reconcile()
	if b = f.only(t); !b.Running || len(f.pulled) != pulls {
		t.Fatalf("restart: %+v, pulls %d→%d", b, pulls, len(f.pulled))
	}

	// Off: the container goes.
	h.Want("scout", Wish{Enabled: false})
	h.Reconcile()
	if len(f.boxes) != 0 {
		t.Fatalf("off: %+v", f.boxes)
	}
}

func TestHalfDoneRollbackIsMended(t *testing.T) {
	h, f, _ := setup(t, "sha256:a")
	h.Want("scout", Wish{Enabled: true})
	f.boxes["x"] = &Box{ID: "x", Name: "opentv-scout-old", Plugin: "scout", Digest: "sha256:a"}
	h.Reconcile()
	if b := f.only(t); b.Name != "opentv-scout" || !b.Running {
		t.Fatalf("mend: %+v", b)
	}
}

func TestUnreleasedAndSettings(t *testing.T) {
	h, f, _ := setup(t, "")
	h.Want("scout", Wish{Enabled: true})
	h.Reconcile()
	if len(f.boxes) != 0 {
		t.Fatal("installed a plugin without a digest")
	}
	if err := h.Want("scout", Wish{Enabled: true, Settings: map[string]string{"EVIL": "1"}}); err == nil {
		t.Fatal("took a setting the catalog does not name")
	}
	if err := h.Want("nope", Wish{Enabled: true}); err == nil {
		t.Fatal("took an unknown plugin")
	}
}

func TestSignature(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	key := base64.StdEncoding.EncodeToString(pub)
	data, sig := signed(t, priv, Catalog{Version: 2, Plugins: []Plugin{{ID: "scout", Image: "i", Digest: "sha256:a"}}})
	if _, err := verify(data, sig, key); err != nil {
		t.Fatal(err)
	}
	bad := append([]byte{}, data...)
	bad[len(bad)-2] ^= 1
	if _, err := verify(bad, sig, key); err == nil {
		t.Fatal("changed catalog accepted")
	}
	data, sig = signed(t, priv, Catalog{Version: 2, Plugins: []Plugin{{ID: "scout", Image: "i", Digest: "latest"}}})
	if _, err := verify(data, sig, key); err == nil {
		t.Fatal("unpinned image accepted")
	}
	if _, err := verify(data, sig, publicKey); err == nil {
		t.Fatal("test key matched the real one")
	}
}

func TestSpec(t *testing.T) {
	s := spec(Plugin{ID: "scout", Image: "i", Digest: "sha256:a", Port: 1, Env: map[string]string{"TZ": "Europe/Berlin", "A": "x"},
		Volumes: map[string]string{"data": "/app/data"}}, Wish{Settings: map[string]string{"A": "y"}})
	if fmt.Sprint(s.Env) != "[A=y TZ=Europe/Berlin]" || fmt.Sprint(s.Binds) != "[opentv-scout-data:/app/data]" {
		t.Fatalf("%+v", s)
	}
}

func TestForeignContainerOfTheSameNameIsSetAside(t *testing.T) {
	h, f, _ := setup(t, "sha256:a")
	f.boxes["hand"] = &Box{ID: "hand", Name: "opentv-scout", Running: true}
	if err := h.Want("scout", Wish{Enabled: true}); err != nil {
		t.Fatal(err)
	}
	h.Reconcile()
	old := f.boxes["hand"]
	if old == nil || old.Name != "opentv-scout-vorher" || old.Running {
		t.Fatalf("hand-made container not set aside: %+v", old)
	}
	var ours *Box
	for _, b := range f.boxes {
		if b.Name == "opentv-scout" {
			ours = b
		}
	}
	if ours == nil || ours.Plugin != "scout" || !ours.Running {
		t.Fatalf("hub's own Scout not running: %+v", f.boxes)
	}
	if note := h.Status()["plugins"].([]Report)[0].Note; note != "" {
		t.Fatalf("note left: %s", note)
	}
}

func TestSwitchKeepsSettings(t *testing.T) {
	h, _, _ := setup(t, "sha256:a")
	h.catalog.Plugins[0].Settings = []string{"X"}
	if err := h.Want("scout", Wish{Enabled: true, Settings: map[string]string{"X": "1"}}); err != nil {
		t.Fatal(err)
	}
	if err := h.Want("scout", Wish{Enabled: false}); err != nil {
		t.Fatal(err)
	}
	if got := h.wishes["scout"].Settings["X"]; got != "1" {
		t.Fatalf("setting lost on the switch: %q", got)
	}
}
