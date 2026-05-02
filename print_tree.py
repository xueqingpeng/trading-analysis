import sys
from pathlib import Path

def print_tree(directory: Path, file_obj, prefix: str = ""):
    """Recursively prints the directory structure to a file."""
    if not directory.is_dir():
        file_obj.write(f"Error: '{directory}' is not a valid directory.\n")
        return

    # Gather all entries, sorting directories first, then files alphabetically
    entries = sorted(
        list(directory.iterdir()), 
        key=lambda x: (x.is_file(), x.name.lower())
    )
    entries_count = len(entries)

    for i, entry in enumerate(entries):
        is_last = (i == entries_count - 1)
        connector = "└── " if is_last else "├── "
        file_obj.write(f"{prefix}{connector}{entry.name}\n")

        if entry.is_dir():
            extension = "    " if is_last else "│   "
            print_tree(entry, file_obj, prefix=prefix + extension)

if __name__ == "__main__":
    # Argument 1: Target directory (defaults to results/auditing)
    target_path = sys.argv[1] if len(sys.argv) > 1 else "results/auditing"
    # Argument 2: Output text file (defaults to tree_output.txt)
    output_file = sys.argv[2] if len(sys.argv) > 2 else "tree_output.txt"
    
    target_dir = Path(target_path)
    
    # Using utf-8 encoding is vital on Windows so the tree characters render correctly
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"Directory structure for: {target_dir.resolve()}\n")
        f.write("="*50 + "\n")
        
        if target_dir.exists():
            f.write(target_dir.name + "/\n")
            print_tree(target_dir, f)
            print(f"Success! Directory tree saved to {output_file}")
        else:
            msg = f"Directory '{target_dir}' does not exist. Please check the path.\n"
            f.write(msg)
            print(msg)