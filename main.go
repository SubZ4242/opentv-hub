// OpenTV Hub: a small manager on a friend's NAS. It installs the OpenTV plugins the iPhone app
// switches on, each its own container pinned by a signed catalog, updates them once a day with a
// health check, and rolls an update back when it does not come up.
package main

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"log"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Version is set at build time (-X main.Version=…).
var Version = "0.1.2"

const defaultCatalog = "https://raw.githubusercontent.com/SubZ4242/opentv-hub/main/catalog/catalog.json"

func env(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func main() {
	log.SetFlags(log.Ldate | log.Ltime)
	data := env("HUB_DATA", "/data")
	if err := os.MkdirAll(data, 0o700); err != nil {
		log.Fatal(err)
	}
	token := hubToken(data)
	h := newHub(data, newDocker(env("DOCKER_SOCKET", "/var/run/docker.sock")), env("CATALOG_URL", defaultCatalog), publicKey)

	go func() {
		for {
			if err := h.Refresh(); err != nil {
				log.Printf("catalog: %v", err)
			}
			h.Reconcile()
			// Once a day, at a moment of its own: not every hub at GitHub's door at once.
			jitter, _ := rand.Int(rand.Reader, big.NewInt(int64(2*time.Hour)))
			time.Sleep(23*time.Hour + time.Duration(jitter.Int64()))
		}
	}()
	go func() {
		// Between the daily runs: a plugin stopped by hand or by a NAS restart comes back.
		for range time.Tick(10 * time.Minute) {
			h.Reconcile()
		}
	}()

	addr := ":" + env("HUB_PORT", "35100")
	log.Printf("opentv-hub %s on %s", Version, addr)
	log.Fatal(http.ListenAndServe(addr, routes(h, token)))
}

// hubToken is HUB_TOKEN (the iPhone app makes one and puts it into the install text), else one
// made here once and kept.
func hubToken(data string) string {
	if t := env("HUB_TOKEN", ""); t != "" {
		return t
	}
	path := filepath.Join(data, "token")
	if b, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(b))) > 0 {
		return strings.TrimSpace(string(b))
	}
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	t := base64.RawURLEncoding.EncodeToString(b)
	_ = writeFile(path, []byte(t))
	log.Printf("no HUB_TOKEN given: made one, it is in %s", path)
	return t
}

func routes(h *Hub, token string) http.Handler {
	mux := http.NewServeMux()
	send := func(w http.ResponseWriter, code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(v)
	}
	auth := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
				send(w, 401, map[string]string{"error": "unauthorized"})
				return
			}
			next(w, r)
		}
	}
	// Open: the app finds hubs on the network with it.
	mux.HandleFunc("GET /api/hello", func(w http.ResponseWriter, r *http.Request) {
		send(w, 200, map[string]string{"app": "opentv-hub", "version": Version})
	})
	mux.HandleFunc("GET /api/status", auth(func(w http.ResponseWriter, r *http.Request) {
		send(w, 200, h.Status())
	}))
	mux.HandleFunc("PUT /api/plugins/{id}", auth(func(w http.ResponseWriter, r *http.Request) {
		var wish Wish
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&wish); err != nil {
			send(w, 400, map[string]string{"error": "json"})
			return
		}
		if err := h.Want(r.PathValue("id"), wish); err != nil {
			send(w, 400, map[string]string{"error": err.Error()})
			return
		}
		go h.Reconcile()
		send(w, 202, h.Status())
	}))
	mux.HandleFunc("POST /api/update", auth(func(w http.ResponseWriter, r *http.Request) {
		go func() {
			if err := h.Refresh(); err != nil {
				log.Printf("catalog: %v", err)
			}
			h.Reconcile()
		}()
		send(w, 202, map[string]bool{"ok": true})
	}))
	return mux
}
