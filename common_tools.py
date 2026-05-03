# ~/home/test.py
import json
import ast
from fastmcp import FastMCP

mcp = FastMCP("Common Tools")

@mcp.tool
def evaluate_literal(expression: str):
    """
    Safely evaluates a string expression containing Python literals
    (numbers, strings, tuples, lists, dicts).
    Returns the evaluated object or raises an exception if invalid.
    """
    try:
        # ast.literal_eval converts a string representation of a literal structure
        # into the actual Python object.
        result = ast.literal_eval(expression)
        return result
    except (ValueError, SyntaxError) as e:
        return(f"Error evaluating expression: {e}")

@mcp.tool
def count_letters_and_return_formatted_string(word):
    """
    Counts the frequency of each letter in a word and returns the results
    as a custom formatted string (e.g., "a:5\nb:6").

    Args:
        word (str): The input string/word.

    Returns:
        str: A string containing all counts, one per line.
    """
    counts = {}
    lower_word = word.lower()

    # 1. Count the letters manually
    for char in lower_word:
        if char.isalpha():
            if char in counts:
                counts[char] += 1
            else:
                counts[char] = 1

    # 2. Build the custom formatted string
    output_lines = []

    # Iterate through the dictionary items (key, value)
    for letter, count in counts.items():
        # Format each line as "letter:count"
        line = f"{letter}:{count}"
        output_lines.append(line)

    # Join all lines with a newline character ('\n') to create the final string
    return f"Occurrences of letters\n {" ".join(output_lines)}"

if __name__ == "__main__":
    mcp.run()
