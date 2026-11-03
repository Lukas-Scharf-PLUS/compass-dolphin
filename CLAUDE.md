# CLAUDE.md

This file tells Claude how to work in this project. Read it at the start of every session and follow it.

---

## 1. Project overview

- **What this project does:** This project develops an InSAR pipeline using COMPASS and dolphin specifically designed for the city of Vienna. Data downloads are not included in this project. It expects that SLCs, orbit files, DEM and any other additional files to exist already. 
- **Tech stack:** Python, libraries that are needed for the aim

## 2. Access to the codebase

- You may **read any file** in this repository to understand context before making changes.
- Before changing code, look at related files (callers, tests, config) so changes fit the existing design.
- Do **not** read or print secrets. Treat files like `.env`, `*.key`, `*.pem` and credential files as off-limits for output — never copy their values into code, logs, commits or chat.
- Ask before deleting files, renaming many files, or running commands that are hard to undo (e.g. dropping databases, force-pushing, mass refactors).

---

## 3. How to write code

### Clarity first
- Write code that is **easy to read and understand**. Prefer simple and obvious over clever and short.
- Use **descriptive names** for variables, functions, classes and files. `calculate_monthly_total()` beats `calc()`.
- Keep functions **small and focused** — one function, one job. If a function needs a long comment to explain *what* it does, consider splitting it.
- Avoid deep nesting. Use early returns to keep the main logic flat.
- Avoid magic numbers and strings — put them in named constants.
- Don't add abstractions, patterns or dependencies "for later". Add them when they are actually needed.

### Consistency
- Follow the style, naming and patterns **already used in the codebase**.
- If there is a formatter or linter configured, follow it.
- Don't reformat unrelated code in the same change.

### Error handling
- Handle errors explicitly and give clear, helpful error messages.
- Never silently swallow errors.

### Dependencies
- Prefer the standard library and existing dependencies.
- Ask before adding a new library, and explain why it's needed.

---

## 4. Comments

The goal: someone new to the project should understand what the code does and **why**.

- Add a short **header comment at the top of each file** explaining what the file is for.
- Add a **docstring / doc comment to every function, class and module** describing:
  - what it does,
  - its parameters and return value,
  - any errors it can raise or important side effects.
- Use inline comments to explain **why** something is done, especially for non-obvious logic, workarounds, or business rules. Don't comment the obvious (`i += 1  # add one to i`).
- Keep comments **up to date** — when you change code, update the comments that describe it.
- Mark unfinished work clearly with `TODO:` and a short explanation.

---

## 5. File structure

- Keep the file structure **simple, flat and predictable**.
- Group files by feature or purpose, with folder names that make their content obvious.
- One main concept per file. Split files that grow too large or do too many things.
- Put tests in a clear, consistent place (e.g. a `tests/` folder mirroring the source structure).
- Keep configuration in the project root or a dedicated `config/` folder.
- Before creating a new file or folder, check whether an existing one is the right place.
- When you add or move files, update the structure section in `README.md`.

---

## 6. Documentation (README.md)

`README.md` is the main documentation for this project. Keep it accurate.

It should always contain:
1. **What the project is** and what it does.
2. **How to install and set it up** (requirements, environment variables — names only, never values).
3. **How to run it** and **how to run tests**.
4. **Project structure** — a short overview of the main folders and files and what they contain.
5. **How it works** — the main parts of the system and how they fit together.
6. **How to use it** — key features, examples, API endpoints or commands.

Rules:
- **Update `README.md` whenever a change affects** setup, usage, structure, configuration or behavior.
- Write in plain language. Explain it as you would to a new developer joining the project.
- Use examples where they help.

---

## 7. Workflow

1. **Understand first.** Read the relevant code and ask questions if the request is unclear before writing lots of code.
2. **Plan bigger changes.** For anything beyond a small fix, briefly describe your plan before implementing.
3. **Make small, focused changes.** One logical change at a time.
4. **Test.** Add or update tests for new behavior and bug fixes. Run the tests and make sure they pass.
5. **Update docs and comments** as part of the same change.
6. **Summarize.** At the end, briefly explain what you changed, which files were touched, and anything I should check or decide.

---

## 8. Communication

- I'm learning, so **explain your reasoning** briefly when you make non-obvious decisions.
- If there are several reasonable approaches, mention the trade-offs and recommend one.
- If you're unsure about something, say so instead of guessing.
- Point out bugs, security issues or bad practices you notice, even outside the current task.

---

## 9. Git (if used)

- Write clear commit messages: a short summary line, then details if needed.
- Don't commit secrets, build output or dependency folders (keep `.gitignore` up to date).
- Ask before committing, pushing, or changing branches.
