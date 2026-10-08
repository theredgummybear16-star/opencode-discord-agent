import json
import urllib.request


def payload_commands():
    return [
        {"name": "help", "description": "List all owner commands"},
        {"name": "status", "description": "Bot status: uptime, tenants, crons, servers, model"},
        {"name": "cronjobs", "description": "List all background cron jobs"},
        {"name": "run", "description": "Run a workspace script now",
         "options": [
             {"name": "file", "description": "script filename", "type": 3, "required": True},
             {"name": "tid", "description": "tenant id (default owner dm)", "type": 3, "required": False},
         ]},
        {"name": "logs", "description": "Last N decrypted log lines",
         "options": [{"name": "n", "description": "number of lines", "type": 4, "required": False}]},
        {"name": "tenants", "description": "List tenants"},
        {"name": "memory", "description": "Show a tenant's MEMORY.md",
         "options": [{"name": "tid", "description": "tenant id", "type": 3, "required": False}]},
        {"name": "clear", "description": "Reset a tenant session",
         "options": [{"name": "target", "description": "tid or 'all'", "type": 3, "required": False}]},
        {"name": "model", "description": "Show or change the model",
         "options": [{"name": "name", "description": "model id", "type": 3, "required": False}]},
        {"name": "ssh", "description": "Cloudflared SSH + web terminal",
         "options": [
             {"name": "action", "description": "what to do", "type": 3, "required": True,
              "choices": [
                  {"name": "on", "value": "on"},
                  {"name": "off", "value": "off"},
                  {"name": "status", "value": "status"},
                  {"name": "pass", "value": "pass"},
              ]},
         ]},
        {"name": "restart", "description": "Restart the whole agent instance"},
        {"name": "stop", "description": "Shut the instance down until the next scheduled run"},
        {"name": "invite", "description": "Get the bot invite link (with slash-command scope)"},
    ]


def register_commands(app_id, token):
    payload = json.dumps(payload_commands()).encode()
    url = "https://discord.com/api/v10/applications/%s/commands" % int(app_id)
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": "Bot " + token,
            "Content-Type": "application/json",
            "User-Agent": "opencode-ai-agent/1.0",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status