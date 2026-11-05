# Signal action integrations

The public endpoint is `GET https://<your-winner-predict-domain>/api/v1/signals/latest/action`. For local development, use `http://127.0.0.1:8000` as the base URL. The request needs no API key.

When a published signal is available, the response contains exactly these two fields:

```json
{"action":"OK","index":"#20063"}
```

```json
{"action":"WAIT","index":"#20064"}
```

The action is a projection of the latest canonical published signal: published `BET` maps to `OK`, and a genuinely published `WAIT` remains `WAIT`. The index comes only from that signal's actual `target_round_index`, formatted as `#` plus the round number. All three read endpoints use the repository's newest-first publication ordering. New durable records get a server-side publication timestamp; publisher-supplied decision timestamps do not reorder new records. No Rules Engine or collector decision is run during polling, and polling does not publish signals. `OK` reflects the published decision; it does not guarantee a winning round. The current canonical publisher accepts `BET` decisions and does not publish `WAIT` decisions.

Before the first eligible published signal (or if the latest signal cannot be projected), the response matches `GET /api/v1/signals/latest` no-data behavior:

```json
{"ok":true,"signal":null}
```

Treat a response without an `action` property as `NO_SIGNAL_YET`. The endpoint never substitutes `source_round_index`, timestamps, or database IDs. The endpoint keeps its existing anonymous public-read policy, HTTPS/origin checks, and rate limit.

## JavaScript

```js
const baseUrl = "https://<your-winner-predict-domain>";
const response = await fetch(`${baseUrl}/api/v1/signals/latest/action`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);

const payload = await response.json();
if (!payload || typeof payload !== "object" || !("action" in payload)) {
  console.log("NO_SIGNAL_YET");
} else {
  const { action, index } = payload;
  if (action !== "OK" && action !== "WAIT") throw new Error("Invalid action");
  if (typeof index !== "string" || !/^#[1-9][0-9]*$/.test(index)) {
    throw new Error("Invalid round index");
  }
  if (action === "OK") {
    console.log(`Decision OK for ${index}`);
  } else {
    console.log(`WAIT for ${index}`);
  }
}
```

## TypeScript

```ts
type SignalAction = { action: "OK" | "WAIT"; index: string };

const baseUrl = "https://<your-winner-predict-domain>";
const response = await fetch(`${baseUrl}/api/v1/signals/latest/action`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);

const payload: unknown = await response.json();
if (!payload || typeof payload !== "object" || !("action" in payload)) {
  console.log("NO_SIGNAL_YET");
} else {
  const { action, index } = payload as SignalAction;
  if (action !== "OK" && action !== "WAIT") throw new Error("Invalid action");
  if (typeof index !== "string" || !/^#[1-9][0-9]*$/.test(index)) {
    throw new Error("Invalid round index");
  }
  if (action === "OK") {
    console.log(`Decision OK for ${index}`);
  } else {
    console.log(`WAIT for ${index}`);
  }
}
```

## Python

```python
import json
import re
import urllib.request

base_url = "https://<your-winner-predict-domain>"
request = urllib.request.Request(f"{base_url}/api/v1/signals/latest/action")
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)

if "action" not in payload:
    print("NO_SIGNAL_YET")
    raise SystemExit(0)
action = payload.get("action")
index = payload.get("index")
if action not in ("OK", "WAIT"):
    raise ValueError("Invalid action")
if not isinstance(index, str) or not re.fullmatch(r"#[1-9][0-9]*", index):
    raise ValueError("Invalid round index")
if action == "OK":
    print(f"Decision OK for {index}")
else:
    print(f"WAIT for {index}")
```

## PHP

```php
<?php
$baseUrl = 'https://<your-winner-predict-domain>';
$handle = curl_init($baseUrl . '/api/v1/signals/latest/action');
if ($handle === false) throw new RuntimeException('Could not create HTTP request');
curl_setopt_array($handle, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 10]);
$body = curl_exec($handle);
if ($body === false || curl_getinfo($handle, CURLINFO_RESPONSE_CODE) >= 400) {
    throw new RuntimeException('Signal API request failed');
}
curl_close($handle);

$payload = json_decode($body, true, flags: JSON_THROW_ON_ERROR);
if (!is_array($payload) || !array_key_exists('action', $payload)) {
    echo "NO_SIGNAL_YET\n";
    exit;
}
$action = $payload['action'] ?? null;
$index = $payload['index'] ?? null;
if (!in_array($action, ['OK', 'WAIT'], true)) {
    throw new RuntimeException('Invalid action');
}
if (!is_string($index) || !preg_match('/^#[1-9][0-9]*$/', $index)) {
    throw new RuntimeException('Invalid round index');
}
if ($action === 'OK') {
    echo "Decision OK for {$index}\n";
} else {
    echo "WAIT for {$index}\n";
}
```

## Java

This example uses Jackson Databind for JSON parsing.

```java
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

public class SignalActionExample {
    public static void main(String[] args) throws Exception {
        String baseUrl = "https://<your-winner-predict-domain>";
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + "/api/v1/signals/latest/action"))
            .GET().build();
        HttpResponse<String> response = HttpClient.newHttpClient().send(
            request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IllegalStateException("HTTP " + response.statusCode());
        }

        JsonNode payload = new ObjectMapper().readTree(response.body());
        if (payload == null || !payload.has("action")) {
            System.out.println("NO_SIGNAL_YET");
            return;
        }
        String action = payload.path("action").asText();
        JsonNode indexNode = payload.get("index");
        String index = indexNode != null && indexNode.isTextual() ? indexNode.asText() : "";
        if (!action.equals("OK") && !action.equals("WAIT")) {
            throw new IllegalStateException("Invalid action");
        }
        if (!index.matches("^#[1-9][0-9]*$")) {
            throw new IllegalStateException("Invalid round index");
        }
        if (action.equals("OK")) {
            System.out.println("Decision OK for " + index);
        } else {
            System.out.println("WAIT for " + index);
        }
    }
}
```

## C#

```csharp
using System;
using System.Net.Http;
using System.Text.Json;
using System.Text.RegularExpressions;

const string baseUrl = "https://<your-winner-predict-domain>";
using var client = new HttpClient();
using var response = await client.GetAsync(baseUrl + "/api/v1/signals/latest/action");
response.EnsureSuccessStatusCode();
using var payload = JsonDocument.Parse(await response.Content.ReadAsStringAsync());

var root = payload.RootElement;
if (!root.TryGetProperty("action", out var actionElement))
{
    Console.WriteLine("NO_SIGNAL_YET");
    return;
}
var action = actionElement.GetString();
var indexElement = root.GetProperty("index");
var index = indexElement.ValueKind == JsonValueKind.String ? indexElement.GetString() : null;
if (action is not ("OK" or "WAIT")) throw new InvalidOperationException("Invalid action");
if (index is null || !Regex.IsMatch(index, "^#[1-9][0-9]*$"))
    throw new InvalidOperationException("Invalid round index");
if (action == "OK")
{
    Console.WriteLine($"Decision OK for {index}");
}
else
{
    Console.WriteLine($"WAIT for {index}");
}
```

## cURL

This shell example requires `curl` and `jq` and validates the response while extracting only `action` and `index`.

```bash
set -euo pipefail
BASE_URL='https://<your-winner-predict-domain>'
result=$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/signals/latest/action" \
  | jq -er 'if (has("action") | not) then "NO_SIGNAL_YET" else
      if ((.action == "OK" or .action == "WAIT") and
      ((.index | type) == "string" and (.index | test("^#[1-9][0-9]*$"))))
      then [.action, .index] | @tsv else error("invalid action response") end end')
if [ "$result" = "NO_SIGNAL_YET" ]; then
  printf 'NO_SIGNAL_YET\n'
  exit 0
fi
IFS="$(printf '\t')" read -r action index <<EOF
$result
EOF
if [ "$action" = "OK" ]; then
  printf 'Decision OK for %s\n' "$index"
else
  printf 'WAIT for %s\n' "$index"
fi
```
