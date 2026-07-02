# Directions for Claude

@README.md
@BOT_PLAN.md
@BOT_TODO.md

## Purpose of project

This repo is a python project for aun automated bot that trades calendar options on crypto curreny with Deribit.

## Instructions

### Create test files

- Create new testing code for any new code you create
- If necessary update existing test code to support any updates made to existing modules
- Fix code for any broken tests that arise from newly created code

### Create debug files

- for any new code created, create scripts with a name of `scratch_*.py` within the scratch folder
- scrath files demonstrate the new functionality, proving to the human that the code works as intended
- scratch files provide debug capability
- scratch files are not unit tests and they establish real connections and perform real actions when paper trading
- scratch files do not perform real trades. They do not run when `DERIBIT_PAPER = False`

### Update README.md if needed

Make sure the Readme.md file is up to date as well.

### Update progress in BOT_TODO.md

Each time you complete a task from BOT_TODO, make sure to update progress.

### Commiting changes

- do not commit changes to git unless explicitly asked
- when asked to commit, the comment must not contain your name, the name of any model, or any reference to AI
- when asked to commit and you have been working on a separate branch, asking if that branch should be merged into the main branch
- do not sign commits with <noreply@anthropic.com> credentials, any reference yourself or any email address at all
