# Gmail → Google Sheet + Drive + AI Draft Replies

A Google Apps Script that watches your Gmail inbox and, for every new unread email:

1. **Logs a row** to a Google Sheet (date, sender, subject, body, attachment Yes/No, links).
2. **Saves attachments** to a Drive folder named by date (`DD-MM-YYYY`).
3. **Drafts an AI reply** (Groq or Gemini) when the email warrants one — saved as a **draft** for you to review and send.
4. **Labels the thread** (`Sheet-Logged`) so it is never processed twice.

### Features
- **Send Status column** — shows `⏳ Pending owner to send` when a draft is created, and
  automatically flips to `✅ Sent successfully` once you send the draft.
- **Activity Log tab** — a live, timestamped feed of everything the script does, including a
  `No new incoming emails` line on empty runs. Keep the Sheet open to watch it update live.
- **Professional formatting** — coloured headers, frozen header row, filter, banded rows,
  sensible column widths, wrapped body text, and status colour-coding.

---

## One-time spreadsheet setup
1. Open the bound Google Sheet (or set `SHEET_ID` in `GmailToSheet.gs`).
2. In the Apps Script editor run **`setApiKey()`** once (after pasting your key), then delete the key from the function.
   - Free Groq key: <https://console.groq.com/keys>
   - Free Gemini key: <https://aistudio.google.com/apikey>
3. Run **`installTrigger()`** once to schedule `processEmails()` every 5 minutes.
4. (Optional) Run **`reformatSheets()`** to apply the professional look to an existing sheet.

---

## GitHub auto-deploy (GitHub = source of truth → Apps Script)

This repo is wired so that **every push to `main` redeploys the code to your Apps Script
project** via [`clasp`](https://github.com/google/clasp) and GitHub Actions.

### Prerequisites (one time, on your PC)
You currently have **git** but not **Node.js**. Install Node LTS, then clasp:

```powershell
# 1. Install Node.js LTS from https://nodejs.org  (or: winget install OpenJS.NodeJS.LTS)
# 2. Install clasp globally
npm install -g @google/clasp
# 3. Enable the Apps Script API for your account:  https://script.google.com/home/usersettings  (turn it ON)
# 4. Log in (opens a browser)
clasp login
```

`clasp login` writes credentials to `C:\Users\<you>\.clasprc.json`. **Never commit this file**
(it is already in `.gitignore`).

### Link this repo to your Apps Script project
1. In the Apps Script editor: **Project Settings → IDs → copy the Script ID**.
2. Paste it into `.clasp.json` replacing `PASTE_YOUR_SCRIPT_ID_HERE`.
3. Adjust `timeZone` in `appsscript.json` if you are not in `Asia/Karachi`.
4. Test a manual deploy from your PC:
   ```powershell
   clasp push --force
   ```

### Push the repo to GitHub
```powershell
git add .
git commit -m "Gmail-to-Sheet automation"
# create an empty repo on github.com first, then:
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### Make the GitHub Action able to deploy
The Action needs your clasp credentials as a secret:
1. Open `C:\Users\<you>\.clasprc.json`, copy its **entire contents**.
2. On GitHub: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `CLASPRC_JSON`
   - Value: paste the file contents.
3. Done. From now on every `git push` to `main` runs `.github/workflows/deploy.yml`,
   which installs clasp, restores your credentials, and runs `clasp push --force`.

> **Direction of truth:** edit code locally / in GitHub and push → it deploys to Apps Script.
> If you ever edit directly in the Apps Script web editor, run `clasp pull` to bring those
> changes back into the repo before your next push (otherwise the push overwrites them).

---

## Files
| File | Purpose |
|------|---------|
| `GmailToSheet.gs` | The automation. |
| `appsscript.json` | Apps Script manifest (timezone, runtime). |
| `.clasp.json` | Links the repo to your Apps Script project (needs your Script ID). |
| `.github/workflows/deploy.yml` | Auto-deploy on push to `main`. |
| `.gitignore` | Keeps credentials/noise out of git. |
