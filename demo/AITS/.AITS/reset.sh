#!/bin/bash
# Wipe synthetic transcripts + role file so the demo can be re-seeded.
set -e
python3 "$(dirname "$0")/setup.py" --reset
