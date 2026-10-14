import json
from pathlib import Path

# Define your target directory path
directory_path = r"D:\files\novag7\t_feed_v1-livefeed_v1\refs\feeds"


def format_json_files(folder_path):
  path = Path(folder_path)

  if not path.exists():
    print(f"Error: The path '{folder_path}' does not exist.")
    return

  json_files = list(path.glob("*.json"))

  if not json_files:
    print(f"No JSON files found in: {folder_path}")
    return

  print(f"Found {len(json_files)} JSON files. Cleaning and formatting...\n")

  success_count = 0
  for file_path in json_files:
    try:
      # Read the raw text content of the file
      with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

      # Parse the first valid JSON object and ignore any extra trailing data
      decoder = json.JSONDecoder()
      data, idx = decoder.raw_decode(content.strip())

      # Write back only the valid first JSON object with indent=4
      with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

      print(f"[SUCCESS] {file_path.name} (Trimmed extra data)")
      success_count += 1
    except Exception as e:
      print(f"[ERROR] Failed to process {file_path.name}: {e}")

  print(
      f"\nFinished! Successfully processed {success_count} out of"
      f" {len(json_files)} files."
  )


if __name__ == "__main__":
  format_json_files(directory_path)