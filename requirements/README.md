# Python dependency groups

Install only the group needed for the feature you run:

```bash
python3 -m pip install -r requirements/preference.txt
```

| File | Purpose |
| --- | --- |
| `demo.txt` | Offline demo PDF generation and browser fixture dependencies |
| `cloud.txt` | Exact production image dependencies; shared by Docker and release checks |
| `preference-core.txt` | Lightweight ranking dependencies |
| `preference.txt` | Ranking plus local sentence embeddings |
| `local-mlx.txt` | Optional local Apple Silicon models |
| `outlook.txt` | Outlook authentication and API access |
| `mail-archive.txt` | Encrypted mail archive |
| `resume.txt` | Resume PDF processing |
| `autofill.txt` | Browser autofill integration |

The collector and its core tests remain dependency-free. Keep relative `-r` includes relative to this directory. Refresh the pinned cloud group explicitly when rebuilding production images.
