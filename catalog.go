package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// publicKey checks the catalog: only what its private key signed is ever installed. The private
// key never leaves the owner's machine (tools/catalog keygen).
const publicKey = "MdqMEa/WJcQBwVXILrSf7h10DBme+1yFlKiDFCs9NqU="

// Catalog is what the hub may install: each plugin's image pinned by digest.
type Catalog struct {
	// Version only ever grows: an older catalog (replayed, or a stale cache) is refused.
	Version   int      `json:"version"`
	Published string   `json:"published"`
	Hub       HubEntry `json:"hub"`
	Plugins   []Plugin `json:"plugins"`
}

type HubEntry struct {
	Version string `json:"version"`
	Image   string `json:"image"`
	Digest  string `json:"digest"`
}

type Plugin struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Version string `json:"version"`
	Image   string `json:"image"`
	// Digest pins the image ("sha256:…"); empty while the plugin is not released yet.
	Digest string `json:"digest"`
	// Port is published on the NAS under the same number.
	Port   int    `json:"port"`
	Health string `json:"health"`
	// Env is fixed by the catalog; Settings names the variables the app may set on top.
	Env      map[string]string `json:"env,omitempty"`
	Settings []string          `json:"settings,omitempty"`
	// Volumes: a name (kept across updates, as "opentv-<id>-<name>") to the path in the container.
	Volumes map[string]string `json:"volumes,omitempty"`
}

func (c *Catalog) plugin(id string) *Plugin {
	for i := range c.Plugins {
		if c.Plugins[i].ID == id {
			return &c.Plugins[i]
		}
	}
	return nil
}

// verify parses a catalog after checking its signature (base64 Ed25519 over the exact bytes).
func verify(data, sig []byte, key string) (*Catalog, error) {
	pub, err := base64.StdEncoding.DecodeString(key)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return nil, errors.New("hub has no valid catalog key")
	}
	s, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sig)))
	if err != nil || len(s) != ed25519.SignatureSize {
		return nil, errors.New("catalog signature unreadable")
	}
	if !ed25519.Verify(pub, data, s) {
		return nil, errors.New("catalog signature does not match")
	}
	var c Catalog
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	for _, p := range c.Plugins {
		if p.ID == "" || strings.ContainsAny(p.ID, "/ :") || p.Image == "" {
			return nil, fmt.Errorf("catalog: bad plugin %q", p.ID)
		}
		if p.Digest != "" && !strings.HasPrefix(p.Digest, "sha256:") {
			return nil, fmt.Errorf("catalog: %s is not pinned by digest", p.ID)
		}
	}
	return &c, nil
}

// fetch downloads the catalog and its signature.
func fetch(url string) (data, sig []byte, err error) {
	client := &http.Client{Timeout: 30 * time.Second}
	get := func(u string) ([]byte, error) {
		r, err := client.Get(u)
		if err != nil {
			return nil, err
		}
		defer r.Body.Close()
		if r.StatusCode != 200 {
			return nil, fmt.Errorf("%s: %s", u, r.Status)
		}
		return io.ReadAll(io.LimitReader(r.Body, 1<<20))
	}
	if data, err = get(url); err != nil {
		return nil, nil, err
	}
	if sig, err = get(url + ".sig"); err != nil {
		return nil, nil, err
	}
	return data, sig, nil
}
