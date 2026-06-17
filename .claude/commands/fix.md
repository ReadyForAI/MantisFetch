Fix the following bug in the MantisFetch project.

Before making changes:
1. Reproduce or locate the issue in the codebase
2. Identify the root cause
3. Explain the root cause and your proposed fix to me
4. Wait for my confirmation before proceeding

After I confirm:
1. Create a fix branch: `git checkout -b fix/<short-description>`
2. Apply the fix
3. Add a regression test if applicable
4. Run `ruff check .` and `pytest tests/ -v`
5. Commit with message format: `fix(<scope>): <description>`
6. Push the branch: `git push -u origin HEAD`
7. Show me the summary and ask if I want to create a PR

Bug description: $ARGUMENTS
