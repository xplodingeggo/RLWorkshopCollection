# RL Map Archive

I made an archive with some of my workshop maps i had on my computer because https://rocketleaguemaps.us/ doesnt work anymore and this might help some poeple. Files are hosted on a cloudflare r2 bucket on my website so downloads should be fast and stable.
The original steam workshop links are underneath every map entry and its a steam icon that you click and it takes you there.

## website

[xplodingeggo.github.io/RLWorkshopCollection](https://xplodingeggo.github.io/RLWorkshopCollection/)

## ➕ Adding a map

1. Upload the `.upk` / `.udk` file (and preview image if you have it) to any website where you can download a file from
2. Add an entry to `maps.json`:

```json
{
  "Title": "My Map Name",
  "Author": "YourName",
  "Description": "Short description of the map.",
  "PreviewUrl": "https://archive.org/your-preview-image.jpg",
  "downloadUrl": "https://archive.org/download/your-item/MapName.upk",
  "category": "training",
  "steamUrl": "https://steamcommunity.com/sharedfiles/filedetails/?id=[workshop id]",
  "format": "upk"
}
```
'category' syntax - the category you want has to be either training, dribble, fun or other. Make it lowercase and if you dont know a tag you can just leave it blank or delete the variable.
If you want multiple tags do it like this:
"category": ["fun", "other"],

If you dont know any of the info you can just leave it blank its fine idrc. Just leave it blank as ""

If a map came with a `.json` sidecar file (from the original Workshop upload), just paste those fields in directly — `Title`, `Author`, `Description`, and `PreviewUrl` match exactly. Unless the description is super long then just shorten it or something because it probably wont fit in the box, and if you have time then add in the category and the steamUrl variables

3. Open a pull request to the github

## ⚖️ Legal

Map files belong to their original creators. This archive exists for preservation purposes only. Not affiliated with Psyonix or Epic Games.

## Future additions
i just want to maybe add like a link to the original workshop maps on steam. I forgot to do it when i was making it but oh well.
Also just to clarify i did use AI for most of this.
