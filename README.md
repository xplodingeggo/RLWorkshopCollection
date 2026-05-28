# RL Map Archive

A community-driven archive of Rocket League custom Workshop maps. Free to download, forever.

> Spiritual successor to rocketleaguemaps.us — preserving Workshop maps for the community.

## 🗺 Live Site

[your-domain.com](https://your-domain.com)

## 📁 How it works

- **`index.html`** — the full website, runs entirely client-side
- **`maps.json`** — the map database (title, author, description, preview image, download link)
- Map files are hosted on [Archive.org](https://archive.org) and linked from `maps.json`

## ➕ Adding a map

1. Upload the `.upk` / `.udk` file (and preview image if you have it) to [Archive.org](https://archive.org/upload)
2. Add an entry to `maps.json`:

```json
{
  "Title": "My Map Name",
  "Author": "YourName",
  "Description": "Short description of the map.",
  "PreviewUrl": "https://archive.org/your-preview-image.jpg",
  "downloadUrl": "https://archive.org/download/your-item/MapName.upk",
  "format": "upk"
}
```

3. Open a Pull Request — that's it!

If a map came with a `.json` sidecar file (from the original Workshop upload), just paste those fields in directly — `Title`, `Author`, `Description`, and `PreviewUrl` match exactly.

## 🤝 Contributing map files

Have old Workshop maps saved locally? Please contribute them!

- Upload to Archive.org (free, permanent)
- Submit a PR adding them to `maps.json`
- Or open an Issue with the files and we'll add them

## 🛠 Running locally

No build step needed — just open `index.html` in a browser, or serve with:

```bash
npx serve .
# or
python3 -m http.server
```

## ⚖️ Legal

Map files belong to their original creators. This archive exists for preservation purposes only. Not affiliated with Psyonix or Epic Games.
