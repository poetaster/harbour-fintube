#!/bin/sh
# Every offline test in one go: the engine (Python), the InnerTube parsers against recorded
# YouTube answers, the QML decisions (Node), and the check that no new engine code is untested.
# Run it after any change to python/ or qml/, and always before tagging (see TESTING.md).
# Full engine output: python3 python/test_youfish.py
cd "$(dirname "$0")" || exit 1
log=$(mktemp)
python3 python/test_youfish.py >"$log" 2>&1; engine=$?
if [ "$engine" -eq 0 ]; then
    tail -n 3 "$log" | grep -v '^$'
else
    grep -E '^(FAIL|ERROR):' "$log"; tail -n 1 "$log"
fi
rm -f "$log"
parsers=0
for t in tests/test_*.py; do
    if python3 "$t" >"$log" 2>&1; then echo "$(basename "$t" .py): $(grep '^Ran' "$log") OK"
    else cat "$log"; parsers=1; fi
done
rm -f "$log"
python3 tests/coverage_report.py --check >"$log" 2>&1; cov=$?
if [ "$cov" -eq 0 ]; then tail -n 1 "$log"; else sed -n '/^Now tested\|^UNTESTED\|^  /p' "$log"; fi
rm -f "$log"
if command -v node >/dev/null 2>&1; then
    node tests/qml_logic_test.js; qml=$?
else
    echo "QML logic tests NOT RUN: install Node.js (Fedora: sudo dnf install nodejs)"; qml=1
fi
if [ "$engine" -eq 0 ] && [ "$parsers" -eq 0 ] && [ "$qml" -eq 0 ] && [ "$cov" -eq 0 ]; then echo "ALL TESTS PASSED"; exit 0; fi
echo "TESTS FAILED"; exit 1
