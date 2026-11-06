"""Conversation parsing and local folder exports (standard library only)."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


DESTINATIONS = {}


def parse_script(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Paste a conversation before preparing speech.')
    if len(text) > 10000:
        raise ValueError('Keep the conversation within 10,000 characters.')
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    marker = re.compile(r'^\s*(\d+)\.[ \t]*(.*)$')
    lines = text.split('\n')
    numbered = any(marker.match(line) for line in lines)
    turns = []
    if numbered:
        current = None
        for line in lines:
            match = marker.match(line)
            if match:
                speaker = int(match[1])
                if speaker not in (1, 2, 3):
                    raise ValueError('Use speaker numbers 1., 2., or 3. only.')
                if current is not None:
                    turns.append(current)
                current = {'speaker': speaker, 'text': match[2]}
            elif line.strip():
                if current is None or current['text'].endswith('\n\n'):
                    raise ValueError('Number every speaking turn when using speaker labels. Add 1., 2., or 3. at the start of this paragraph.')
                current['text'] += '\n' + line
            elif current is not None:
                current['text'] = current['text'].rstrip('\n') + '\n\n'
        if current is not None:
            turns.append(current)
    else:
        turns = [{'speaker': i % 2 + 1, 'text': paragraph}
                 for i, paragraph in enumerate(re.split(r'\n[ \t]*\n(?:[ \t]*\n)*', text))]
    if len(turns) > 200:
        raise ValueError('Use at most 200 speaking turns per conversation.')
    for i, turn in enumerate(turns, 1):
        turn['text'] = turn['text'].strip()
        turn['number'] = i
        if not turn['text']:
            raise ValueError(f'Message {i} has a speaker label but no speech. Add text before preparing.')
    return turns


def register_destination(raw_path):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError('Choose a destination folder first.')
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('Choose an existing folder on this computer.')
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError:
        raise ValueError('This folder is not writable. Choose another destination.') from None
    token = uuid.uuid4().hex
    DESTINATIONS[token] = path
    return {'token': token, 'path': str(path)}


def destination_for(token):
    path = DESTINATIONS.get(str(token))
    if path is None or not path.is_dir():
        raise ValueError('Choose the destination folder again before generating.')
    return path


def choose_folder():
    """Use the host's picker; a typed-path option is also available in the UI."""
    try:
        if sys.platform == 'darwin':
            command = ['osascript', '-e', 'POSIX path of (choose folder with prompt "Choose a folder for VO Studio audio")']
        elif os.name == 'nt':
            command = ['powershell', '-NoProfile', '-STA', '-Command',
                       'Add-Type -AssemblyName System.Windows.Forms; '
                       '$picker = New-Object System.Windows.Forms.FolderBrowserDialog; '
                       '$picker.Description = "Choose a folder for VO Studio audio"; '
                       'if ($picker.ShowDialog() -eq "OK") { $picker.SelectedPath }']
        else:
            command = [sys.executable, '-c',
                       'import tkinter as tk; from tkinter import filedialog; '
                       'root=tk.Tk(); root.withdraw(); '
                       'print(filedialog.askdirectory(title="Choose a folder for VO Studio audio")); root.destroy()']
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Folder picker unavailable. Enter the folder path below instead.') from None
    if result.returncode:
        if sys.platform == 'darwin' and '-128' in result.stderr:
            return None
        raise ValueError('Folder picker unavailable. Enter the folder path below instead.')
    path = result.stdout.strip()
    return register_destination(path) if path else None


def export_wav(source, destination, stem):
    """Keep older takes, including files from other projects; never overwrite."""
    for take in range(1, 10001):
        suffix = '' if take == 1 else f'_take_{take:02d}'
        target = destination / (stem + suffix + '.wav')
        try:
            output = target.open('xb')
        except FileExistsError:
            continue
        try:
            with output, source.open('rb') as input_file:
                shutil.copyfileobj(input_file, output)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return target
    raise OSError('Too many takes with this filename. Choose another destination.')
