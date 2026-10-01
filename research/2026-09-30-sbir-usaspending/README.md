# SBIR load and USAspending linking tests, 2026-09-30

Throwaway scripts behind the numbers in `docs/design.md`. They use the Python
standard library only and run in a working directory that holds the data.

1. Download the data:
   `curl -o award_data.csv https://data.www.sbir.gov/mod_awarddatapublic/award_data.csv`
2. `python3 load.py`: loads the CSV into `sbir.db` and prints coverage stats.
   It dropped contact columns during testing; the plugin will keep all 42
   columns.
3. `python3 match.py`: matches a sample of NASA awards to USAspending by
   contract number.
4. `python3 xmatch.py`: matches DoD, DOE, NSF and HHS awards to USAspending
   with per-agency normalisation. Its NIH pattern has a known bug (see the
   design note).

Samples are random, so reruns will vary slightly. The data files are
gitignored.
