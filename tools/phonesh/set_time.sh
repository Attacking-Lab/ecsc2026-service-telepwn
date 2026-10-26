#!/bin/bash

DATE=$(date +%m%d%H%M%Y.%S)
for x in 11 12 13 14 15; do
	python ./phonesh.py --host 10.60.$x.4 --port 23 "date '$DATE'"
done
