#!/bin/bash

apt-get update
apt-get install -y libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 libasound2 libatspi2.0-0 libgtk-3-0

pip install -r requirements.txt

playwright install chromium

python insta.py
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
