from collections import defaultdict
import os

def find_duplicates(env_file=".env"):
    key_lines = defaultdict(list)
    value_lines = defaultdict(list)

    if not os.path.exists(env_file):
        print(f"File not found: {env_file}")
        return

    with open(env_file, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            key_lines[key].append((lineno, value))
            value_lines[value].append((lineno, key))

    print("=== Duplicate KEYS ===")
    found_keys = False
    for key, occurrences in key_lines.items():
        if len(occurrences) > 1:
            found_keys = True
            print(f"Key '{key}' appears {len(occurrences)} times:")
            for lineno, value in occurrences:
                print(f"  line {lineno}: {key}={value}")
    if not found_keys:
        print("  (none)")

    print("\n=== Duplicate VALUES ===")
    found_values = False
    for value, occurrences in value_lines.items():
        if len(occurrences) > 1:
            found_values = True
            print(f"Value '{value}' appears {len(occurrences)} times:")
            for lineno, key in occurrences:
                print(f"  line {lineno}: {key}={value}")
    if not found_values:
        print("  (none)")


if __name__ == "__main__":
    find_duplicates(".env")