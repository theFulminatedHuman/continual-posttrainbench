# startup-outreach

Sends personalized cold emails to startups from your Gmail, with your resume attached. **Nothing is
sent until you approve it.**

For each row in your spreadsheet it:

1. **Researches the company.** It uses the Claude API with web search to find what they build,
   their stage, recent news and open roles.
2. **Drafts a short, plain email** (about 90–150 words). The draft links one specific thing about
   the company to real projects from your resume and mentions your website. It never invents facts
   about you or the company.
3. **Shows you the draft and waits.** You pick:
   `[y]` send · `[e]` edit · `[r]` rewrite with feedback · `[b]` show research · `[s]` skip · `[q]` quit
4. **Sends it from your Gmail** with the resume attached, and adds it to `sent_log.csv`. A company
   or address in that log is never emailed again.

It also caps sends per day (`daily_limit`, default 25) and waits between sends
(`min_seconds_between_sends`, default 60) so Gmail doesn't flag you as spam.

## One-time setup (about 10 minutes)

### 1. Get the two keys

- **Claude API key.** Create one at <https://platform.claude.com/settings/keys> and add some
  credit. As a rough guide, each company costs a few US cents to tens of cents, mostly for the web
  research.
- **Gmail app password.** Turn on 2-Step Verification on your Google account. Then create an app
  password at <https://myaccount.google.com/apppasswords> (any name, e.g. "outreach"). It is
  16 letters. **Do not use your normal Gmail password.**

### 2. Install Python and the script

You need Python 3.10 or newer (on Windows, install it from <https://python.org> and tick
"Add to PATH").

```bash
git clone -b claude/beautiful-edison-3xh7u0 https://github.com/thefulminatedhuman/continual-posttrainbench.git
cd continual-posttrainbench/startup-outreach
pip install -r requirements.txt
```

### 3. Add your files

- Put your resume in this folder as `resume.pdf`.
- Copy `config.example.json` to `config.json` and fill it in: your name, Gmail address, phone,
  website and the roles you want. Use `extra_notes` for true facts that aren't on your resume
  (notice period, relocation, etc.).
- Save your spreadsheet here as `companies.csv` or `companies.xlsx`, and set `companies_file` in
  `config.json` to match. Headers are matched loosely: `Company` / `Company Name`,
  `Email` / `Email ID`, `Founder` / `Contact Name`, `Website`, `Notes`. Only company and email
  are required. See `companies.example.csv`.

These files hold your personal data and are gitignored, so they never get committed.

### 4. Set the keys for this terminal session

macOS / Linux:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export GMAIL_APP_PASSWORD="abcdefghijklmnop"
```

Windows (PowerShell):
```powershell
$env:ANTHROPIC_API_KEY="sk-ant-..."
$env:GMAIL_APP_PASSWORD="abcdefghijklmnop"
```

If `GMAIL_APP_PASSWORD` isn't set, the script asks for it with hidden input.

## Run it

```bash
python outreach.py --dry-run --limit 3   # first: draft 3 emails, send nothing, read drafts/
python outreach.py --limit 5             # then: review and send 5
python outreach.py                       # whole sheet, up to the daily limit
python outreach.py --only "Razorpay"     # just one company
```

Research is cached in `drafts/research/`. Re-running a company doesn't repeat the web search;
delete its file to force fresh research.

### No laptop setup? Use Google Colab

1. Open <https://colab.research.google.com> and start a new notebook.
2. Upload `outreach.py`, `requirements.txt`, `config.json`, `resume.pdf` and your spreadsheet
   with the file panel on the left.
3. Run:

```python
!pip install -q -r requirements.txt
import os
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-..."
os.environ["GMAIL_APP_PASSWORD"] = "abcdefghijklmnop"
!python outreach.py --limit 5
```

The `[y/e/r/s/q]` prompts work in the cell output. In Colab, `[e]` asks you to paste the edited
email, since there is no text editor.

## Tips

- **Read the `CHECK:` line under each draft.** It's where the script flags thin research, a name
  it couldn't confirm, or anything you should double-check.
- **Founder and recruiter addresses work far better** than `info@` or `contact@`. The script
  points out generic inboxes when it sees one.
- **Start slowly.** Send 10–20 a day for the first few days. A new burst of identical-looking
  cold emails is what gets Gmail accounts limited.
- **`[r]` takes plain feedback**, for example "mention my payments project instead" or "shorter,
  less formal".
