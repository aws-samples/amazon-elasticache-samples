"""Fun easter egg tools for novelty and entertainment."""

from strands import tool

# Initialize system memory to 4GB (4096 MB)
system_memory_mb = 4096


@tool
def get_system_memory() -> str:
    """
    Get the current system memory.

    Returns:
        str: The current system memory in MB and GB
    """
    global system_memory_mb
    gb = system_memory_mb / 1024
    return f"Current system memory: {system_memory_mb}MB ({gb:.2f}GB)"


@tool
def download_more_ram(ramToDownloadMB: int) -> str:
    """
    Download more ram

    Args:
        ramToDownloadMB (int): The amount of megabytes of ram to download

    Returns:
        str: status of the operation
    """
    print(f"Downloading {ramToDownloadMB}MB of Ram...")
    global system_memory_mb
    system_memory_mb += ramToDownloadMB
    gb = system_memory_mb / 1024
    return f"Successfully downloaded {ramToDownloadMB}MB of ram. Total system memory is now {system_memory_mb}MB ({gb:.2f}GB). Your computer is now faster"


@tool
def letter_counter(word: str, letter: str) -> int:
    """
    Count occurrences of a specific letter in a word.

    Args:
        word (str): The input word to search in
        letter (str): The specific letter to count

    Returns:
        int: The number of occurrences of the letter in the word
    """
    if not isinstance(word, str) or not isinstance(letter, str):
        return 0

    if len(letter) != 1:
        raise ValueError("The 'letter' parameter must be a single character")

    return word.lower().count(letter.lower())
