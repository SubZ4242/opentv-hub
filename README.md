# OpenTV Hub

Ein kleiner Helfer auf dem NAS, für die OpenTV-Plugins, die nicht auf dem Fernseher rechnen sollen.

Einmal eingerichtet, kümmert er sich selbst:

- installiert die Plugins, die in der OpenTV-App eingeschaltet werden, je als eigenen Container
- holt einmal am Tag Updates, prüft danach, ob das Plugin läuft, und nimmt ein Update zurück, wenn nicht
- installiert nur, was im **signierten Katalog** steht, festgelegt auf den genauen Image-Stand (Digest)

## Einrichten

Die OpenTV-App am iPhone zeigt den Weg für dein NAS und füllt den Schlüssel aus. Von Hand:
[`install/compose.yaml`](install/compose.yaml) als Projekt bzw. Stack anlegen
(TrueNAS: Apps › Custom App › YAML, Synology: Container Manager › Projekt, Portainer: Stack).

Der Hub braucht Zugriff auf Docker (`/var/run/docker.sock`) und läuft im Netz des NAS (Port 35100).

## Plugins

| Plugin | Port | Was es tut |
|---|---|---|
| Entdecken (`scout`) | 35120 | Filme, die noch nicht in der Mediathek sind: Trends, Beliebt, IMDb Top 250 und Empfehlungen passend zur eigenen Mediathek. Nutzt die öffentlichen Listen von Radarrs Metadaten-Dienst. |

## Schnittstelle

`GET /api/hello` (offen), mit `Authorization: Bearer <HUB_TOKEN>`: `GET /api/status`,
`PUT /api/plugins/{id}` mit `{"enabled": true}`, `POST /api/update`.

## Katalog

[`catalog/catalog.json`](catalog/catalog.json) und seine Signatur `catalog.json.sig` (Ed25519). Der
öffentliche Schlüssel steht im Hub ([`catalog.go`](catalog.go)); der private liegt nicht in diesem Repository.
Signiert wird mit `go run ./tools/catalog sign <schlüssel> catalog/catalog.json`.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
