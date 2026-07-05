# awesome-selfhosted PR draft

Target: https://github.com/awesome-selfhosted/awesome-selfhosted
Section: **Photo and Video Galleries** → no; **Media Streaming** → no;
correct section: **Document Management / ... ** → no.
Use section: **Photo/Image Galleries** is for galleries; best fit is
**"Media Management"**-adjacent — actual precedent: Stirling-PDF sits in
"Document Management". Propose under **"Image Processing"** if present,
else **"Miscellaneous"**. (Check the current section list at PR time;
withoutBG's listing section is the precedent to follow.)

**Entry line (alphabetical position under B):**

```markdown
- [Batchroom](https://github.com/ekinarca/batchroom) - Batch AI image processing server: background removal, resizing, format conversion and marketplace preset packs with watch folders, REST API and web UI. No metering, images never leave the server. ([Demo](https://github.com/ekinarca/batchroom#quick-start), [Source Code](https://github.com/ekinarca/batchroom)) `MIT` `Docker/Python`
```

**Checklist before opening the PR (their rules):**

- [ ] Project ≥ 4 months old at PR time (repo age gate — schedule accordingly)
- [ ] Actively maintained, releases tagged
- [ ] `demo` link only if a live demo exists (drop otherwise)
- [ ] One-line description < 250 chars, no marketing superlatives
- [ ] License in their allowed list (MIT ✓)

**alternativeto.net entry (same week):**

- Name: Batchroom
- Alternative to: Photoroom, remove.bg, Adobe Express background remover
- Tags: self-hosted, background-removal, image-processing, batch, docker
- Description: reuse README first paragraph.

**selfh.st newsletter pitch (short email):**

> Subject: Batchroom — self-hosted Photoroom alternative (batch bg
> removal + marketplace presets)
>
> Hi! I just launched Batchroom, an MIT-licensed self-hosted media
> pipeline server: batch background removal, resizing, conversion and
> marketplace preset packs with watch folders and a 10-language web UI.
> Single container, SQLite, CPU-only AI with checksum-pinned models.
> Launch thread got [N] points on HN this week. Might be a fit for the
> new-apps roundup: https://github.com/ekinarca/batchroom
