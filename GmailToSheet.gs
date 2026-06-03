/**
 * Gmail -> Google Sheet + Drive + AI draft replies (Groq / Gemini)
 * ----------------------------------------------------------------
 * For each NEW unread inbox email it:
 *   1. Adds a row to a Google Sheet (sender, subject, body, attachment Yes/No, links)
 *   2. Saves attachments into a Drive folder named by date (DD-MM-YYYY)
 *   3. Asks the AI to decide if the email warrants a reply; if yes, drafts one
 *      and saves it as a DRAFT in the Gmail thread (you review + send manually)
 *   4. Labels the thread so it is never processed twice
 *
 * NEW IN THIS VERSION:
 *   - "Send Status" column: shows "Pending owner to send" when a draft is created,
 *     and AUTOMATICALLY switches to "Sent successfully" once you send the draft.
 *   - "Activity Log" tab: a live, timestamped feed of everything the script does
 *     (including "No new incoming emails" on empty runs). Keep the Sheet open to
 *     watch it update in real time.
 *   - Professional sheet formatting: coloured headers, frozen row, filter, banded
 *     rows, sensible column widths, wrapped body text, status colour-coding.
 *
 * SETUP:
 *   - Run installTrigger() ONCE to run processEmails() every 5 minutes.
 *   - Store your API key:  run setApiKey() once (see bottom),
 *     OR Project Settings > Script Properties > add  GROQ_API_KEY = <your key>.
 *   - Get a free Groq key at https://console.groq.com/keys  (or Gemini: https://aistudio.google.com/apikey)
 *   - To (re)apply the professional formatting to an existing sheet, run reformatSheets().
 */

// ============================== CONFIG ==============================
var SHEET_ID        = '';                    // Paste your Sheet ID, or leave '' to use the bound sheet.
var SHEET_NAME      = 'Gmail Log';
var ACTIVITY_SHEET  = 'Activity Log';        // live feed of what the script is doing
var DRIVE_PARENT    = 'Gmail Attachments';
var PROCESSED_LABEL = 'Sheet-Logged';
var SEARCH_QUERY    = 'in:inbox is:unread';
var MAX_THREADS     = 10;                     // keep small to stay under AI free-tier per-minute limits
var MAX_BODY_CHARS  = 49000;
var AI_THROTTLE_MS  = 4500;                   // pause between AI calls (~13/min, under the ~15/min free cap)
var MAX_RETRIES     = 4;                      // retries on HTTP 429 (rate limit), with backoff
var MAX_ACTIVITY_ROWS = 1000;                // Activity Log keeps at most this many rows (oldest trimmed)

// --- AI / reply settings ---
var PROVIDER        = 'groq';                // 'groq' (free, recommended) or 'gemini'
var GEMINI_MODEL    = 'gemini-2.0-flash';    // used when PROVIDER = 'gemini'
var GROQ_MODEL      = 'llama-3.3-70b-versatile';  // used when PROVIDER = 'groq'
var OWNER_NAME      = 'Abdul Samad';         // who the replies are "from"
var SIGNATURE       = 'Best regards,\nAbdul Samad';

// >>> EDIT THIS: tone & rules for the AI. This is your reply "template/persona". <<<
var REPLY_GUIDELINES = [
  'You draft email replies on behalf of ' + OWNER_NAME + '.',
  'Decide whether the incoming email genuinely warrants a personal reply.',
  'Do NOT reply to: newsletters, marketing/promotions, notifications, receipts,',
  '  automated/system messages, no-reply senders, or pure FYI messages.',
  'DO reply to: real questions, enquiries, requests, or messages that expect a response.',
  '',
  'When you DO reply, follow these style rules:',
  '- Professional, warm, and concise (3-6 short sentences).',
  '- Address the person by their first name when known.',
  '- Acknowledge their message, answer or confirm next steps, and set expectations.',
  '- If you cannot fully answer, say it will be reviewed and a follow-up will come shortly.',
  '- Do NOT invent facts, prices, dates, or commitments.',
  '- End with this signature exactly:\n' + SIGNATURE
].join('\n');

// --- Sheet column layout (1-based). 10 visible + 2 hidden tracking columns. ---
var COL_DATE         = 1;
var COL_SENDER_NAME  = 2;
var COL_SENDER_EMAIL = 3;
var COL_SUBJECT      = 4;
var COL_BODY         = 5;
var COL_ATTACH       = 6;
var COL_LINKS        = 7;
var COL_REPLY_STATUS = 8;
var COL_SEND_STATUS  = 9;
var COL_NOTES        = 10;
var COL_THREAD_ID    = 11;   // hidden
var COL_DRAFT_ID     = 12;   // hidden
var TOTAL_COLS       = 12;

var HEADERS = ['Date Received', 'Sender Name', 'Sender Email', 'Subject', 'Email Body',
               'Attachment', 'Attachment Links', 'Reply Status', 'Send Status', 'Reply Notes',
               'Thread Id', 'Draft Id'];

// --- Status text ---
var SEND_PENDING = '⏳ Pending owner to send';   // ⏳
var SEND_SENT    = '✅ Sent successfully';        // ✅

// --- Colour palette (professional) ---
var CLR_HEADER_BG = '#0b5394';   // dark blue header
var CLR_HEADER_FG = '#ffffff';
var CLR_DRAFT     = '#d9ead3';   // light green  -> Draft created
var CLR_SKIP      = '#efefef';   // light grey   -> Skipped
var CLR_ERROR     = '#f4cccc';   // light red    -> Error
var CLR_PENDING   = '#fff2cc';   // amber        -> Pending owner to send
var CLR_SENT      = '#b6d7a8';   // green        -> Sent successfully
// ===================================================================


/** MAIN: called by the time trigger. */
function processEmails() {
  var label = getOrCreateLabel_(PROCESSED_LABEL);
  var sheet = getSheet_();

  // 1) Refresh "Send Status" for any drafts the owner has sent since the last run.
  updateSendStatuses_(sheet);

  // 2) Look for new mail.
  var query   = SEARCH_QUERY + ' -label:' + PROCESSED_LABEL;
  var threads = GmailApp.search(query, 0, MAX_THREADS);

  // 3) No new mail -> log it to the Activity Log (no email is sent) and stop.
  if (threads.length === 0) {
    logActivity_('No new incoming emails', 'Checked the inbox — nothing new.');
    trimActivityLog_();
    Logger.log('No new incoming emails.');
    return;
  }

  logActivity_('Run started', threads.length + ' new thread(s) found');
  for (var t = 0; t < threads.length; t++) {
    var messages = threads[t].getMessages();
    for (var m = 0; m < messages.length; m++) {
      var isLast = (m === messages.length - 1);   // only the latest message gets a reply draft
      logMessage_(sheet, messages[m], isLast);
    }
    threads[t].addLabel(label);
  }
  logActivity_('Run finished', 'Processed ' + threads.length + ' thread(s)');
  trimActivityLog_();

  Logger.log('Processed ' + threads.length + ' thread(s).');
}


/** Write one email message as a row; save attachments; maybe draft an AI reply. */
function logMessage_(sheet, msg, attemptReply) {
  var from         = msg.getFrom();
  var senderName   = parseName_(from);
  var senderEmail  = parseEmail_(from);
  var subject      = msg.getSubject() || '(no subject)';
  var body         = (msg.getPlainBody() || '').substring(0, MAX_BODY_CHARS);
  var dateReceived = msg.getDate();
  var threadId     = msg.getThread().getId();

  // --- attachments ---
  var attachments  = msg.getAttachments();
  var hasAttach    = attachments.length > 0 ? 'Yes' : 'No';
  var links        = [];
  if (attachments.length > 0) {
    var folder = getDateFolder_(dateReceived);
    for (var a = 0; a < attachments.length; a++) {
      var file = folder.createFile(attachments[a].copyBlob());
      links.push(file.getUrl());
    }
    logActivity_('Attachments saved', attachments.length + ' file(s) for "' + subject + '"');
  }

  // --- AI reply decision ---
  var replyStatus = '-';
  var sendStatus  = '';
  var replyNotes  = '';
  var draftId     = '';
  if (attemptReply) {
    if (isAutomatedSender_(senderEmail)) {
      replyStatus = 'Skipped';
      replyNotes  = 'Automated / no-reply sender';
    } else {
      try {
        Utilities.sleep(AI_THROTTLE_MS);   // throttle to respect free-tier per-minute limit
        var ai = generateReply_(senderName, senderEmail, subject, body);
        if (ai.shouldReply && ai.reply) {
          var draft = msg.createDraftReply(ai.reply);   // DRAFT only — not sent
          draftId     = draft.getId();
          replyStatus = 'Draft created';
          sendStatus  = SEND_PENDING;
          replyNotes  = ai.reason || '';
          logActivity_('Draft created', '"' + subject + '" from ' + senderEmail);
        } else {
          replyStatus = 'Skipped';
          replyNotes  = ai.reason || 'AI judged no reply needed';
        }
      } catch (e) {
        replyStatus = 'Error';
        replyNotes  = String(e).substring(0, 500);
        logActivity_('Error', '"' + subject + '": ' + replyNotes);
      }
    }
  }

  sheet.appendRow([
    Utilities.formatDate(dateReceived, Session.getScriptTimeZone(), 'dd-MM-yyyy HH:mm'),
    senderName,
    senderEmail,
    subject,
    body,
    hasAttach,
    links.join('\n'),
    replyStatus,
    sendStatus,
    replyNotes,
    threadId,
    draftId
  ]);

  applyRowFormat_(sheet, sheet.getLastRow(), replyStatus, sendStatus);
}


/**
 * Re-check every "Pending owner to send" row: if the stored draft no longer
 * exists in the account's drafts, the owner has sent (or discarded) it, so we
 * flip the Send Status to "Sent successfully".
 *
 * Note: Gmail gives no direct "was this draft sent vs deleted" signal, so a
 * manually-deleted draft would also show as sent. In normal use the only reason
 * a drafted reply disappears is that you sent it.
 */
function updateSendStatuses_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  // Build a set of currently-existing draft ids.
  var liveDraftIds = {};
  var drafts = GmailApp.getDrafts();
  for (var i = 0; i < drafts.length; i++) liveDraftIds[drafts[i].getId()] = true;

  var n         = lastRow - 1;
  var sendVals  = sheet.getRange(2, COL_SEND_STATUS, n, 1).getValues();
  var draftVals = sheet.getRange(2, COL_DRAFT_ID,    n, 1).getValues();

  var updated = 0;
  for (var r = 0; r < n; r++) {
    if (sendVals[r][0] !== SEND_PENDING) continue;
    var did = draftVals[r][0];
    if (did && !liveDraftIds[did]) {
      var rowNum = r + 2;
      sheet.getRange(rowNum, COL_SEND_STATUS).setValue(SEND_SENT).setBackground(CLR_SENT);
      updated++;
      logActivity_('Reply sent', 'Owner sent the draft on row ' + rowNum);
    }
  }
  if (updated > 0) Logger.log('Switched ' + updated + ' row(s) to "Sent successfully".');
}


// ============================== AI PROVIDERS ==============================

/** Decide relevance and (if relevant) draft a reply. Routes to the configured provider. */
function generateReply_(senderName, senderEmail, subject, body) {
  var emailBlock =
    '--- INCOMING EMAIL ---' +
    '\nFrom: ' + senderName + ' <' + senderEmail + '>' +
    '\nSubject: ' + subject +
    '\nBody:\n' + body.substring(0, 8000) +
    '\n--- END EMAIL ---';

  if (PROVIDER === 'groq') return callGroq_(REPLY_GUIDELINES, emailBlock);
  return callGemini_(REPLY_GUIDELINES, emailBlock);
}


/** Groq (free) chat completion -> {shouldReply, reason, reply}. */
function callGroq_(systemText, emailBlock) {
  var apiKey = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
  if (!apiKey) throw new Error('GROQ_API_KEY not set. Run setApiKey() or add it in Script Properties.');

  var system = systemText +
    '\n\nRespond ONLY with a JSON object of the form ' +
    '{"shouldReply": boolean, "reason": string, "reply": string}. ' +
    'If shouldReply is false, set reply to an empty string.';

  var payload = {
    model: GROQ_MODEL,
    temperature: 0.4,
    response_format: { type: 'json_object' },
    messages: [
      { role: 'system', content: system },
      { role: 'user',   content: emailBlock }
    ]
  };

  var resp, code;
  for (var attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    resp = UrlFetchApp.fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + apiKey },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    code = resp.getResponseCode();
    if (code === 200) break;
    if ((code === 429 || code === 503) && attempt < MAX_RETRIES) {
      Utilities.sleep(Math.pow(2, attempt) * 3000);
      continue;
    }
    throw new Error('Groq HTTP ' + code + ': ' + resp.getContentText().substring(0, 300));
  }

  var data = JSON.parse(resp.getContentText());
  var text = data.choices && data.choices[0] && data.choices[0].message.content;
  if (!text) throw new Error('Empty Groq response');
  return JSON.parse(text);
}


/** Gemini chat completion -> {shouldReply, reason, reply}. */
function callGemini_(systemText, emailBlock) {
  var apiKey = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
  if (!apiKey) throw new Error('GEMINI_API_KEY not set. Run setApiKey() or add it in Script Properties.');

  var prompt = systemText + '\n\n' + emailBlock;

  var payload = {
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: 0.4,
      responseMimeType: 'application/json',
      responseSchema: {
        type: 'OBJECT',
        properties: {
          shouldReply: { type: 'BOOLEAN' },
          reason:      { type: 'STRING' },
          reply:       { type: 'STRING' }
        },
        required: ['shouldReply', 'reason']
      }
    }
  };

  var url = 'https://generativelanguage.googleapis.com/v1beta/models/' +
            GEMINI_MODEL + ':generateContent?key=' + apiKey;

  var resp, code;
  for (var attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    code = resp.getResponseCode();

    if (code === 200) break;

    // 429 = rate limited, 503 = overloaded -> wait and retry with exponential backoff
    if ((code === 429 || code === 503) && attempt < MAX_RETRIES) {
      Utilities.sleep(Math.pow(2, attempt) * 3000);   // 3s, 6s, 12s, 24s
      continue;
    }
    throw new Error('Gemini HTTP ' + code + ': ' + resp.getContentText().substring(0, 300));
  }

  var data = JSON.parse(resp.getContentText());
  var text = data.candidates && data.candidates[0] &&
             data.candidates[0].content.parts[0].text;
  if (!text) throw new Error('Empty Gemini response');

  return JSON.parse(text);   // {shouldReply, reason, reply}
}


// ============================== SHEETS / FORMATTING ==============================

function getSpreadsheet_() {
  var ss = SHEET_ID ? SpreadsheetApp.openById(SHEET_ID) : SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('No spreadsheet found. Set SHEET_ID in CONFIG.');
  return ss;
}

function getSheet_() {
  var ss = getSpreadsheet_();
  var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    formatSheet_(sheet);
  }
  return sheet;
}

/** Apply the professional look to the main log sheet (header colours, widths, banding, filter). */
function formatSheet_(sheet) {
  // Header styling
  sheet.getRange(1, 1, 1, TOTAL_COLS)
    .setBackground(CLR_HEADER_BG)
    .setFontColor(CLR_HEADER_FG)
    .setFontWeight('bold')
    .setFontFamily('Arial')
    .setHorizontalAlignment('center')
    .setVerticalAlignment('middle');
  sheet.setRowHeight(1, 32);
  sheet.setFrozenRows(1);

  // Column widths (cols 11 & 12 are hidden tracking columns)
  var widths = [150, 150, 210, 240, 360, 90, 220, 130, 175, 260, 40, 40];
  for (var i = 0; i < widths.length; i++) sheet.setColumnWidth(i + 1, widths[i]);

  // Hide the tracking columns used to detect "sent"
  sheet.hideColumns(COL_THREAD_ID);
  sheet.hideColumns(COL_DRAFT_ID);

  // Alternating row colours (banding) over the data area
  var bands = sheet.getBandings();
  for (var b = 0; b < bands.length; b++) bands[b].remove();
  var maxRows = Math.max(sheet.getMaxRows(), 2);
  sheet.getRange(2, 1, maxRows - 1, TOTAL_COLS)
    .applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);

  // Wrap long text and top-align everything in the data area
  sheet.getRange(2, 1, maxRows - 1, TOTAL_COLS).setVerticalAlignment('top');
  sheet.getRange(2, COL_BODY,  maxRows - 1, 1).setWrap(true);
  sheet.getRange(2, COL_NOTES, maxRows - 1, 1).setWrap(true);

  // Filter on the visible columns
  var existing = sheet.getFilter();
  if (existing) existing.remove();
  var filterRows = Math.max(sheet.getLastRow(), 1);
  sheet.getRange(1, 1, filterRows, COL_NOTES).createFilter();
}

/** Colour-code the status cells of a single freshly-written row. */
function applyRowFormat_(sheet, row, replyStatus, sendStatus) {
  sheet.getRange(row, 1, 1, TOTAL_COLS).setVerticalAlignment('top');
  sheet.getRange(row, COL_BODY).setWrap(true);
  sheet.getRange(row, COL_NOTES).setWrap(true);

  if (replyStatus === 'Draft created')      sheet.getRange(row, COL_REPLY_STATUS).setBackground(CLR_DRAFT);
  else if (replyStatus === 'Skipped')       sheet.getRange(row, COL_REPLY_STATUS).setBackground(CLR_SKIP);
  else if (replyStatus === 'Error')         sheet.getRange(row, COL_REPLY_STATUS).setBackground(CLR_ERROR);

  if (sendStatus === SEND_PENDING)          sheet.getRange(row, COL_SEND_STATUS).setBackground(CLR_PENDING);
  else if (sendStatus === SEND_SENT)        sheet.getRange(row, COL_SEND_STATUS).setBackground(CLR_SENT);
}


// ============================== ACTIVITY LOG ==============================

function getActivitySheet_() {
  var ss = getSpreadsheet_();
  var sheet = ss.getSheetByName(ACTIVITY_SHEET) || ss.insertSheet(ACTIVITY_SHEET);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(['Timestamp', 'Event', 'Details']);
    formatActivitySheet_(sheet);
  }
  return sheet;
}

function formatActivitySheet_(sheet) {
  sheet.getRange(1, 1, 1, 3)
    .setBackground(CLR_HEADER_BG)
    .setFontColor(CLR_HEADER_FG)
    .setFontWeight('bold')
    .setFontFamily('Arial')
    .setVerticalAlignment('middle');
  sheet.setRowHeight(1, 30);
  sheet.setFrozenRows(1);
  sheet.setColumnWidth(1, 170);
  sheet.setColumnWidth(2, 190);
  sheet.setColumnWidth(3, 520);

  var bands = sheet.getBandings();
  for (var b = 0; b < bands.length; b++) bands[b].remove();
  var maxRows = Math.max(sheet.getMaxRows(), 2);
  sheet.getRange(2, 1, maxRows - 1, 3)
    .applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);
}

/** Append one line to the live Activity Log. */
function logActivity_(event, details) {
  var sheet = getActivitySheet_();
  sheet.appendRow([nowStr_(), event, details || '']);
}

/** Keep the Activity Log from growing forever: trim the oldest rows past MAX_ACTIVITY_ROWS. */
function trimActivityLog_() {
  var sheet = getActivitySheet_();
  var dataRows = sheet.getLastRow() - 1;        // minus header
  if (dataRows > MAX_ACTIVITY_ROWS) {
    sheet.deleteRows(2, dataRows - MAX_ACTIVITY_ROWS);
  }
}


// ============================== HELPERS ==============================

function nowStr_() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'dd-MM-yyyy HH:mm:ss');
}

function getParentFolder_() {
  var it = DriveApp.getFoldersByName(DRIVE_PARENT);
  return it.hasNext() ? it.next() : DriveApp.createFolder(DRIVE_PARENT);
}

function getDateFolder_(date) {
  var name   = Utilities.formatDate(date, Session.getScriptTimeZone(), 'dd-MM-yyyy');
  var parent = getParentFolder_();
  var it     = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

function parseName_(from) {
  var m = from.match(/^\s*"?([^"<]*?)"?\s*<.*>$/);
  if (m && m[1].trim()) return m[1].trim();
  return from.replace(/<.*>/, '').trim() || from;
}

function parseEmail_(from) {
  var m = from.match(/<([^>]+)>/);
  return (m ? m[1] : from).toLowerCase();
}

/** Cheap guard so we never draft replies to robots / no-reply addresses. */
function isAutomatedSender_(email) {
  return /(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notification|notifications|bounce|automated|donotreply)@/i.test(email);
}


// ============================== SETUP ==============================

/** Run ONCE to schedule processEmails() every 5 minutes. */
function installTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'processEmails') ScriptApp.deleteTrigger(triggers[i]);
  }
  ScriptApp.newTrigger('processEmails').timeBased().everyMinutes(5).create();
  Logger.log('Trigger installed: processEmails runs every 5 minutes.');
}

/** Run ANYTIME to (re)apply the professional formatting to both sheets. */
function reformatSheets() {
  formatSheet_(getSheet_());
  formatActivitySheet_(getActivitySheet_());
  Logger.log('Both sheets reformatted.');
}

/** Run ONCE to store your API key(s) securely. Fill in the one(s) you use, run, then DELETE the keys here. */
function setApiKey() {
  var props = PropertiesService.getScriptProperties();
  var GROQ_KEY   = 'PASTE_YOUR_GROQ_API_KEY_HERE';     // from console.groq.com/keys
  var GEMINI_KEY = '';                                  // optional: from aistudio.google.com/apikey

  if (GROQ_KEY   && GROQ_KEY.indexOf('PASTE') === -1)   props.setProperty('GROQ_API_KEY', GROQ_KEY);
  if (GEMINI_KEY && GEMINI_KEY.indexOf('PASTE') === -1) props.setProperty('GEMINI_API_KEY', GEMINI_KEY);
  Logger.log('API key(s) saved.');
}
