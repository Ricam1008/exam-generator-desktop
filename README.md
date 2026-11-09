# Exam Generator Desktop

Eine lokale Mac-App zum Erstellen und Bearbeiten von Übungsprüfungen aus Lernmaterial. Die Oberfläche ist auf Deutsch; Prüfungen können auf Deutsch oder Englisch erstellt werden.

## Mac-App verwenden

Die aktuelle Entwicklungsversion wird als Apple-Silicon-`.app` und `.dmg` gebaut. Sie enthält den Python-Dienst und die Dokumentwerkzeuge. Für die installierte App sind Python, Node und Rust nicht erforderlich.

1. Die App öffnen und unter **Einstellungen** eine KI-Verbindung einrichten.
2. Unter **Material** einen Ordner oder einzelne Dateien auswählen und **Material prüfen** anklicken.
3. Auswahl und Lehrveranstaltungen prüfen. **Inhalt prüfen** zeigt den tatsächlich erkannten Text; bei PDFs lässt sich lokale OCR ausdrücklich erneut ausführen.
4. Unter **Prüfung erstellen** Quellen, Sprache, Schwierigkeit, Abdeckung und Fragenanzahl festlegen. Zuerst eine kurze Beispielprüfung erstellen.
5. Unter **Lernen** Prüfungen öffnen, Antworten eingeben und bei Bedarf KI-Feedback anfordern. Lernstände werden lokal gespeichert; eine geänderte Antwort verliert ihre alte Bewertung.

Unterstützte Eingaben: PDF, DOCX, PPTX, TXT, Markdown, PNG, JPEG, TIFF und WebP. PDF-Seiten, PowerPoint-Folien und Text-/Word-Abschnitte bleiben als Quellenstellen erhalten, soweit die Extraktion sie liefert. Word-Seitenzahlen werden nicht erfunden.

## KI-Verbindungen

- **Ollama:** installierte Modelle auswählen oder einen Modellnamen eingeben. Ollama muss separat laufen. Ein Ollama-Modell kann lokal oder in der Cloud arbeiten.
- **DeepSeek:** eigenen API-Schlüssel speichern und ein verfügbares Modell auswählen.
- **Kompatible API:** die vollständige API-Basisadresse und den Modellnamen eintragen, beispielsweise einschließlich `/v1`, wenn der Anbieter das verlangt.
- **ChatGPT:** die offizielle Anmeldung im Browser nutzen. Abo-Zugriff und Modellverfügbarkeit hängen vom Konto und der Freigabe ab. Eine vollständige Testantwort bestätigt den Zugriff; die Modellliste allein tut das nicht.

Zugangsdaten werden im macOS-Schlüsselbund gespeichert. Die App wechselt bei Fehlern nicht automatisch zu einem anderen Anbieter oder einer anderen Abrechnung. Eine abweichende KI-Verbindung für offene Antworten kann ausdrücklich gewählt werden.

Ausgewählte Quellentexte werden erst bei der Generierung an die gewählte KI gesendet. Lokale Texterkennung lädt keine Seiten hoch. Cloud-OCR ist nicht Bestandteil dieser Version.

## Originale, Ergebnisse und Datenschutz

- Originaldateien bleiben unverändert. Die App prüft die tatsächlich aufgelösten Ausgabeorte einschließlich symbolischer Links.
- Geänderte Quellen werden neu erkannt; Dateinamen allein dienen nicht als Identität.
- Neue Ergebnisse werden vollständig vorbereitet, bevor sie gültige Ausgaben ersetzen. Vorherige Arbeitsbereiche bleiben als versteckte Sicherung im Ausgabeordner erhalten. Umgruppierte Prüfungen bleiben zusätzlich in `.history` verfügbar.
- Ein Stopp beendet weitere Anfragen und verwirft unvollständige Ausgaben. Bereits angenommene Cloud-Anfragen können trotzdem berechnet werden.
- Normale Protokolle enthalten keine vollständigen Quellen oder KI-Antworten. Die ausdrücklich aktivierbare Diagnose kann diese Inhalte enthalten.
- Die lokale Steuerungs-API erfordert einen Sitzungsschlüssel. Browser-Prüfungen erhalten nur einen auf ihren Arbeitsbereich begrenzten Zugriff.
- Quellen, erzeugte Prüfungen, Zugangsdaten, Modellgewichte und Build-Dateien gehören nicht ins Git-Repository.

OCR und KI können Fehler machen. Quellenhinweise belegen eine überprüfte Textstelle, nicht die fachliche Richtigkeit jeder generierten Frage. Die gemessenen OCR-Ergebnisse in `docs/verification` stammen aus synthetischen Testseiten, nicht aus einem repräsentativen Vorlesungskorpus.

## Entwicklung und Build

Getestet: Apple Silicon, Python 3.12, Node 22 und Tauri 2. Windows ist kein geprüftes Release-Ziel.

Ein isoliertes Python-Umfeld mit `uv` anlegen und die gepinnten Mac-Abhängigkeiten installieren:

```sh
uv venv --python 3.12 .venv-package
uv pip sync --python .venv-package/bin/python backend/requirements-mac.lock
uv pip install --python .venv-package/bin/python --no-deps -e backend
```

Die normale Dokumentverarbeitung verwendet Docling Slim und macOS Vision. Sie benötigt keine großen Sprachmodelle. Der optionale OCR-Vergleich erfordert ein separates Umfeld mit den `benchmark`-Abhängigkeiten und einen ausdrücklich heruntergeladenen lokalen Granite-Snapshot.

Frontend und Desktop-App:

```sh
cd apps/desktop
npm ci
npm run build
```

Für den Entwicklungsstart muss `.venv-package/bin/python` oder `.venv/bin/python` die Backend-Abhängigkeiten enthalten. Alternativ zuerst den gebündelten Dienst bauen. Der Rust-Launcher erkennt den lokalen Dienst bzw. die gebündelte Version und verwendet einen freien lokalen Port.

Mac-Paket aus dem Repository-Hauptordner bauen:

```sh
sh scripts/build-mac-backend.sh
cd apps/desktop
npm run package:mac
```

Die Ausgabe liegt unter `apps/desktop/src-tauri/target/release/bundle/`. Ohne `APPLE_SIGNING_IDENTITY` verwendet der Build eine lokale Ad-hoc-Signatur. Diese versiegelt das Paket, ersetzt aber kein Apple-Developer-Zertifikat und keine Notarisierung für ein öffentliches Release.

## Prüfen

Vom Repository-Hauptordner, mit normalem macOS-Zugriff für OCR und temporäre lokale HTTP-Listener:

```sh
EXAM_DOCUMENT_RUNTIME_TESTS=1 .venv-package/bin/python -m unittest discover -s tests/backend -v
node --test tests/frontend/*.test.cjs
npm --prefix apps/desktop run build
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked --offline
```

Eigenständige Tests des gebündelten Dienstes:

```sh
.venv-package/bin/python scripts/smoke_packaged_documents.py backend/sidecar-bin/exam-backend-v2/exam-backend-v2
.venv-package/bin/python scripts/smoke_packaged_generation.py --backend backend/sidecar-bin/exam-backend-v2/exam-backend-v2 --report /tmp/exam-packaged-generation.json
```

Der zweite Test verwendet einen lokalen simulierten KI-Anbieter und einen einmaligen Dummy-Schlüssel im Schlüsselbund. Er liest keine vorhandenen Zugangsdaten und löscht seinen eigenen Testeintrag wieder. Er ersetzt keinen Live-Test mit einem echten Anbieter oder ChatGPT-Konto.

Aktueller Prüfstand: `docs/UPGRADE_STATUS.md`.
