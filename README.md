# Recent Job Applications MCP

A local Python MCP server and Windows scheduled worker. The example settings target data and software roles in Gauteng and roles explicitly labelled South Africa (Remote). The two starter sources are Samford Solutions and Verve Global Recruitment. Source coverage depends on their public WP Job Manager RSS feeds and vacancy markup. This release checks the items currently exposed by each feed.

## Run on Windows

Clone this repository and copy `settings.example.json` to `settings.json`. Set your name in the local copy. Open PowerShell in the folder containing jobs.py. Use Python 3.11 or later.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe jobs.py preview
```

Read jobs and source_errors separately. A feed outage gives exit code 2. A successful empty scan means no matching listings appeared in the feeds. Inspect titles, locations, date, destination and route. Title and location filters match phrases. The software does not evaluate every experience requirement, application instruction or employment condition. The email body is in deliver() in jobs.py.

## Enable applications

1. Copy the chosen PDF CV beside jobs.py. Set cv in settings.json to its filename. Configure titles, exclusions, locations and the daily UTC limit. A source's recipient_domains list contains exact domains that may receive your CV.
2. Set sender, smtp_host and smtp_port for your mailbox. The sender uses implicit TLS, normally port 465, with SMTP password or app-password login. Your provider must support this login method.
3. Set the mailbox credential locally. This PowerShell example prompts without displaying the password and saves it as a per-user environment variable for the scheduled task. Processes running as that user can read it. For a single interactive run, omit the final line. If the SMTP username differs from the sender, also set JOB_SMTP_USER.
   
   ```powershell
   $credential = Get-Credential -Message 'SMTP mailbox credentials'
   $env:JOB_SMTP_PASSWORD = $credential.GetNetworkCredential().Password
   [Environment]::SetEnvironmentVariable('JOB_SMTP_PASSWORD', $env:JOB_SMTP_PASSWORD, 'User')
   ```
4. Change enabled to true. Run one batch with the first command below. To run every four hours while signed in, run schedule.ps1. Sign out and back in after adding persistent credentials so scheduled processes inherit them. The first scheduled check starts a minute after registration. Keep the extracted folder at the same path.

   ```powershell
   .\.venv\Scripts\python.exe jobs.py apply
   .\schedule.ps1
   .\.venv\Scripts\python.exe jobs.py history
   ```

To pause sending, set enabled to false. To remove the task, run Unregister-ScheduledTask -TaskName 'Recent Job Applications'. View the task's Last Run Result in Task Scheduler. Run preview or apply interactively to see feed errors.

## Connect an MCP client

For a client supporting local stdio servers, replace both example paths with the absolute paths on your computer. Restart the client after setting mailbox credentials.

```json
{"mcpServers":{"jobs":{"command":"C:\\Tools\\job-apply-mcp\\.venv\\Scripts\\python.exe","args":["C:\\Tools\\job-apply-mcp\\jobs.py","server"]}}}
```

Tools are find_recent_jobs, apply_to_recent_jobs and application_history. The scheduler can run independently of an MCP client.

## Delivery rules

The scraper checks each matching feed item against the vacancy page. Automatic delivery requires a recognised application block explicitly naming one permitted email destination. Other matching vacancies appear with a manual link. Redirects, inaccessible pages and malformed feeds are reported as source errors. Additional WP Job Manager sources can be configured with an HTTPS RSS URL, listing URL prefix and approved recipient domains.

Keep applications.sqlite3 between runs. It reserves each vacancy URL before sending and applies the UTC daily limit across concurrent workers. A mail server acceptance records the handoff. Interrupted or uncertain sends remain reserved for manual reconciliation with your mailbox. Deduplication uses the vacancy URL, so separate recruiters advertising the same underlying role can appear separately.

## Verification

Run the offline regression checks with the virtual environment's Python: `python -m unittest -v`. They use synthetic listings and a simulated mailbox.

## FirstRand Workday pilot

With the same virtual environment, run these commands on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe workday_pilot.py pilot
```

This interactive pilot targets AI Engineer-1 (R55281) only. It stops at Review. Use `server` in place of `pilot` for the MCP stdio interface. The scheduled email worker runs independently.

Copy `workday_profile.example.json` to `workday_profile.json` and replace every example value with your own before using experience corrections. Without that local file the pilot leaves those fields for manual review. Use this pilot only for the matching requisition while it is available.

## What has been verified

The offline workflow test checks stale-listing rejection, email versus manual routing, attachment bytes, duplicate suppression and the daily cap with a simulated SMTP server. A Windows installation authenticated to Gmail and completed a scheduled scan. A user verified that the Workday pilot recognised Review and stopped on a live draft. End-to-end automated browser upload, field correction and employer delivery are unverified.
