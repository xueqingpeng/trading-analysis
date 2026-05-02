import sys
from pathlib import Path

def combine_auditing_files(input_dir: str, output_file: str):
    dir_path = Path(input_dir)
    
    if not dir_path.is_dir():
        print(f"Error: The directory '{dir_path.resolve()}' does not exist.")
        return

    # Find all JSON files and sort them alphabetically
    json_files = sorted(list(dir_path.glob("*.json")))
    
    if not json_files:
        print(f"No JSON files found in '{dir_path}'.")
        return

    print(f"Found {len(json_files)} JSON files. Combining...")

    # Open the output file using utf-8 encoding to prevent Windows character errors
    with open(output_file, 'w', encoding='utf-8') as out_f:
        out_f.write(f"Combined Auditing Results\n")
        out_f.write("=" * 50 + "\n\n")

        for file_path in json_files:
            # Write the filename as a distinct header
            out_f.write(f"--- {file_path.name} ---\n")
            
            try:
                # Read the file content and write it below the header
                content = file_path.read_text(encoding='utf-8').strip()
                out_f.write(content)
                out_f.write("\n\n")
            except Exception as e:
                out_f.write(f"[Error reading file: {e}]\n\n")

    print(f"Success! Combined contents saved to '{output_file}'.")

if __name__ == "__main__":
    # Defaults: Input from results/auditing, output to combined_auditing_results.txt
    input_directory = sys.argv[1] if len(sys.argv) > 1 else "results/auditing"
    output_filename = sys.argv[2] if len(sys.argv) > 2 else "combined_auditing_results.txt"
    
    combine_auditing_files(input_directory, output_filename)