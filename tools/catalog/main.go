// Signs the hub's catalog. The private key stays on the owner's machine, never in this repository.
//
//	go run ./tools/catalog keygen ~/.config/opentv-hub/catalog.key   (prints the public key for catalog.go)
//	go run ./tools/catalog sign   ~/.config/opentv-hub/catalog.key catalog/catalog.json
//	go run ./tools/catalog verify <public key> catalog/catalog.json
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func main() {
	if len(os.Args) < 3 {
		fail("usage: keygen <key> | sign <key> <catalog> | verify <public> <catalog>")
	}
	switch os.Args[1] {
	case "keygen":
		path := os.Args[2]
		if _, err := os.Stat(path); err == nil {
			fail(path + " exists already: a new key would orphan every hub out there")
		}
		pub, priv, err := ed25519.GenerateKey(rand.Reader)
		check(err)
		check(os.MkdirAll(filepath.Dir(path), 0o700))
		check(os.WriteFile(path, []byte(base64.StdEncoding.EncodeToString(priv.Seed())+"\n"), 0o600))
		fmt.Println(base64.StdEncoding.EncodeToString(pub))
	case "sign":
		need(4)
		seed, err := os.ReadFile(os.Args[2])
		check(err)
		s, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(seed)))
		check(err)
		data, err := os.ReadFile(os.Args[3])
		check(err)
		var probe map[string]any
		check(json.Unmarshal(data, &probe)) // never sign a broken catalog
		sig := ed25519.Sign(ed25519.NewKeyFromSeed(s), data)
		check(os.WriteFile(os.Args[3]+".sig", []byte(base64.StdEncoding.EncodeToString(sig)+"\n"), 0o644))
		fmt.Println("signed", os.Args[3])
	case "verify":
		need(4)
		pub, err := base64.StdEncoding.DecodeString(os.Args[2])
		check(err)
		data, err := os.ReadFile(os.Args[3])
		check(err)
		sigText, err := os.ReadFile(os.Args[3] + ".sig")
		check(err)
		sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigText)))
		check(err)
		if !ed25519.Verify(pub, data, sig) {
			fail("signature does not match")
		}
		fmt.Println("ok")
	default:
		fail("unknown command " + os.Args[1])
	}
}

func need(n int) {
	if len(os.Args) < n {
		fail("missing arguments")
	}
}

func check(err error) {
	if err != nil {
		fail(err.Error())
	}
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(1)
}
