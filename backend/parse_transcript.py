import json

path = r"C:\Users\AARYAN KALE\.gemini\antigravity\brain\5bfedaad-4105-4e14-9c45-c0ec614a1ac5\.system_generated\logs\transcript.jsonl"

with open(path, "r", encoding="utf-8") as f:
    for line in f:
        try:
            data = json.loads(line)
            source = data.get("source")
            step_type = data.get("type")
            content = data.get("content")
            if source == "USER_EXPLICIT" or step_type == "USER_INPUT":
                print(f"[USER] {content}\n")
            elif source == "SYSTEM" and "error" in str(content).lower():
                print(f"[SYSTEM ERROR] {content}\n")
        except Exception as e:
            pass
