package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Engine is the part of Docker the hub uses (a fake one in the tests).
type Engine interface {
	Pull(image, digest string) error
	List() ([]Box, error)
	Create(name string, spec Spec) (string, error)
	Start(id string) error
	Stop(id string) error
	Remove(id string) error
	Rename(id, name string) error
}

// Box is one of the hub's containers.
type Box struct {
	ID      string
	Name    string
	Plugin  string
	Digest  string
	Running bool
}

// Spec is a plugin container to make.
type Spec struct {
	Plugin, Image, Digest string
	Env                   []string
	Port                  int
	Binds                 []string
}

const label = "opentv.hub.plugin"

// docker speaks to the Docker Engine API over its socket.
type docker struct{ c *http.Client }

func newDocker(socket string) *docker {
	return &docker{c: &http.Client{
		Timeout: 10 * time.Minute, // a pull of a big image
		Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", socket)
		}},
	}}
}

func (d *docker) call(method, path string, body, out any) error {
	var in io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		in = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, "http://docker"+path, in)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	r, err := d.c.Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if r.StatusCode == 304 {
		return nil // already started or stopped
	}
	if r.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
		return fmt.Errorf("docker %s %s: %s %s", method, path, r.Status, strings.TrimSpace(string(msg)))
	}
	if out == nil {
		_, err = io.Copy(io.Discard, r.Body)
		return err
	}
	return json.NewDecoder(r.Body).Decode(out)
}

func (d *docker) Pull(image, digest string) error {
	req, err := http.NewRequest("POST", "http://docker/images/create?fromImage="+url.QueryEscape(image)+"&tag="+url.QueryEscape(digest), nil)
	if err != nil {
		return err
	}
	r, err := d.c.Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if r.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
		return fmt.Errorf("pull %s: %s %s", image, r.Status, strings.TrimSpace(string(msg)))
	}
	// The answer streams progress; a failure comes as {"error": …} on the way.
	s := bufio.NewScanner(r.Body)
	s.Buffer(make([]byte, 64<<10), 1<<20)
	for s.Scan() {
		var line struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(s.Bytes(), &line) == nil && line.Error != "" {
			return fmt.Errorf("pull %s: %s", image, line.Error)
		}
	}
	return s.Err()
}

func (d *docker) List() ([]Box, error) {
	var raw []struct {
		ID     string            `json:"Id"`
		Names  []string          `json:"Names"`
		Labels map[string]string `json:"Labels"`
		State  string            `json:"State"`
	}
	filter := url.QueryEscape(`{"label":["` + label + `"]}`)
	if err := d.call("GET", "/containers/json?all=1&filters="+filter, nil, &raw); err != nil {
		return nil, err
	}
	out := make([]Box, 0, len(raw))
	for _, c := range raw {
		name := ""
		if len(c.Names) > 0 {
			name = strings.TrimPrefix(c.Names[0], "/")
		}
		out = append(out, Box{ID: c.ID, Name: name, Plugin: c.Labels[label], Digest: c.Labels["opentv.hub.digest"], Running: c.State == "running"})
	}
	return out, nil
}

func (d *docker) Create(name string, s Spec) (string, error) {
	port := fmt.Sprintf("%d/tcp", s.Port)
	host := map[string]any{
		"RestartPolicy": map[string]any{"Name": "unless-stopped"},
		"Binds":         s.Binds,
		"LogConfig":     map[string]any{"Type": "json-file", "Config": map[string]string{"max-size": "5m", "max-file": "2"}},
	}
	body := map[string]any{
		"Image":  s.Image + "@" + s.Digest,
		"Env":    s.Env,
		"Labels": map[string]string{label: s.Plugin, "opentv.hub.digest": s.Digest},
	}
	if s.Port > 0 {
		body["ExposedPorts"] = map[string]any{port: struct{}{}}
		host["PortBindings"] = map[string]any{port: []map[string]string{{"HostPort": fmt.Sprint(s.Port)}}}
	}
	body["HostConfig"] = host
	var out struct {
		ID string `json:"Id"`
	}
	err := d.call("POST", "/containers/create?name="+url.QueryEscape(name), body, &out)
	return out.ID, err
}

func (d *docker) Start(id string) error { return d.call("POST", "/containers/"+id+"/start", nil, nil) }
func (d *docker) Stop(id string) error {
	return d.call("POST", "/containers/"+id+"/stop?t=15", nil, nil)
}
func (d *docker) Remove(id string) error {
	return d.call("DELETE", "/containers/"+id+"?force=1", nil, nil)
}
func (d *docker) Rename(id, name string) error {
	return d.call("POST", "/containers/"+id+"/rename?name="+url.QueryEscape(name), nil, nil)
}
