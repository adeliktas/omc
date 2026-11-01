# Fleet intake {stamp}: local Qwen retrieval and evaluation

You are a local model. Raw Jira, Confluence and Mattermost content must stay on this machine; never paste it into your replies. WORK = {work}. Never write to Jira, Confluence or Mattermost. Keep replies short. Pull only what changed since {since}.

## 1. Browser session
Use the camoufox MCP tools. Call `list_sessions` and reuse a profile that is already logged in, trying each one. Open each site and record whether you are logged in:
- {jira_url}
- {confluence_url}
- {mattermost_url}
For a site that is not logged in, write WORK/BLOCKED-<site>.md saying the owner must log in to that site in a camoufox profile, and continue with the other sites.

## 2. Pull raw data (keep it local)
Use `evaluate` with an in-page `fetch()` (same-origin; the browser cookies authenticate) and return JSON text. Create WORK/raw/{stamp}/ and write exactly these three files there (chmod 600), each a JSON array. Write `[]` for a source you could not reach. Long tool output is saved to a file by the harness; transform that file with Python instead of retyping it.
- **WORK/raw/{stamp}/confluence.json**: `[{{"id", "title", "text", "tasks"}}]`. Page through `/rest/api/content/search?cql=space%3D{confluence_space}%20and%20lastmodified%3E%3D%22{since}%22&expand=body.storage&limit=25&start=N`. In the JS, strip tags from the body (`text`, cut to 6000 characters) and list the texts of `<ac:task-status>incomplete</ac:task-status>` tasks (`tasks`).
- **WORK/raw/{stamp}/jira.json**: `[{{"key", "summary", "status", "type", "priority", "labels", "parent", "updated", "description"}}]`. Page through `/rest/api/2/search?jql=project%3D{jira_project}%20AND%20updated%3E%3D%22{since}%22%20ORDER%20BY%20updated%20DESC&fields=summary,status,issuetype,priority,labels,description,parent,updated&maxResults=50&startAt=N`. In the JS, flatten the fields to plain strings and cut each description to 1500 characters.
- **WORK/raw/{stamp}/mattermost.json**: `[{{"id", "channel", "create_at", "message"}}]` (only if logged in). Send header `X-Requested-With: XMLHttpRequest`.
  1. Call `/api/v4/users/me/teams`.
  2. Chat history: list `/api/v4/users/me/teams/{{team_id}}/channels`. Use only channels of type `O` or `P`, never direct or group messages (`D`, `G`), whose name or display name contains one of {channel_terms}. For each, fetch `/api/v4/channels/{{id}}/posts?since=<epoch ms of {since}T00:00Z>`, at most 500 posts per channel.
  3. Search as well: `POST /api/v4/teams/{{team_id}}/posts/search` with JSON `{{"terms": "<term>", "is_or_search": false}}` and the header `X-CSRF-Token` with the value of the `MMCSRF` cookie (read it from `document.cookie`). Use the terms {search_terms}. Drop results from `D`/`G` channels and duplicates.
  4. Keep only posts created since {since}; cut messages to 1500 characters.

## 3. Finish
Stop once the file your prompt names is written; the coordinator script counts the files and writes DONE. Do not summarize, evaluate, create todos, GitHub issues or board tasks: the coordinator script does that locally.
