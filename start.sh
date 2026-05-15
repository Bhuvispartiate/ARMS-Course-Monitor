#!/bin/bash
python3 dashboard.py &
python3 api_monitor.py &
wait
