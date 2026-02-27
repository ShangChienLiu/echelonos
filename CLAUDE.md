# Project Rules

## 1. Git Commit & Push on Changes

When fixing bugs or creating new features, **always** git commit and git push to the GitHub repository. Every meaningful change should be committed with a clear message and pushed immediately.

**Do NOT add `Co-Authored-By` lines to commit messages.**

## 2. Bug Fix Workflow: E2E Tests First

When fixing bugs:
1. **Write an end-to-end test first** that reproduces and verifies the bug
2. Run the test to confirm it fails (proving the bug exists)
3. Fix the bug
4. Run all end-to-end tests to confirm they pass
5. Only then report the fix as complete
