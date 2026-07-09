# AI Brain

An offline, privacy-first personal AI knowledge system.

AI Brain indexes your local files, extracts text from supported document formats, stores metadata in SQLite, and provides fast keyword search. It is designed as the foundation for a future local AI assistant with semantic search and LLM integration.

---

## Features

### Current (v1)

- Recursive file scanning
- Incremental indexing
- Automatic deleted-file cleanup
- SHA-256 duplicate detection foundation
- MIME type detection
- ZIP archive inspection
- Nested ZIP inspection
- SQLite metadata database
- Full-text search
- Filename search

### Supported File Types

- TXT
- Markdown (.md)
- JSON
- PDF
- DOCX

---

## Roadmap

### v2
- Text chunking
- Embeddings
- Vector database (FAISS/ChromaDB)
- Semantic search
- Retrieval-Augmented Generation (RAG)

### v3
- OCR
- Speech-to-Text
- Image understanding
- Knowledge graph

### Future

- Local LLM integration
- NotebookLM-style workspace
- ChatGPT-style interface
- Plugin architecture
- Multi-user support

---

## Project Structure

```
AI_Brain/
├── ingestion/
├── parsers/
├── database/
├── scripts/
├── tests/
├── docs/
├── config/
└── vectorstore/
```

---

## Installation

```bash
python -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

---

## Usage

### Index files

```bash
python -m ingestion.scanner
```

### Search

```bash
python scripts/search.py
```

---

## Design Goals

- Local-first
- Privacy-first
- Extensible
- Cross-platform
- AI-ready

---

## License

MIT (or your preferred license)