# Science Tutoring Tool — the code 🔬⚙️

This is the **code** behind my [science cheat sheets](https://github.com/clairehjx/science-cheatsheets).
It's a Python pipeline that reads real PSLE science exam papers and turns them into
tidy, friendly study guides — one per topic.

> 🎨 The **finished cheat sheets** you can read live in a separate repo:
> [`science-cheatsheets`](https://github.com/clairehjx/science-cheatsheets).
> 🔒 The **exam papers** are copyrighted, so they live in a separate **private**
> repo: `science-banks` (see *Exam papers* below).

## The big idea (how I broke the problem down)

1. **Read** each exam PDF and pull out the questions → `pdf_extractor.py`
2. **Sort** the questions by topic and **score** how hard they are → `pipeline.py`
3. **Pick** the most important questions and **build** a cheat-sheet web page → `pipeline.py`
4. **Add pictures** of the real questions, cropped straight from the PDFs → `top_questions_images.py`
5. There's even a second track that makes **practice quizzes** → `quiz_generator.py`, `oe_quiz_generator.py`

Each step is one small, clear job. The computer does the slow, repetitive work so
studying is faster. 📚

> 📐 The full architecture (every script, model, and step) is written up in
> [`CLAUDE.md`](CLAUDE.md) — that's my design document.

## Setting it up

1. Install Python 3, then `pip install -r requirements.txt`
2. Set your Gemini API key as an **environment variable** (not a file):
   ```bash
   export GEMINI_API_KEY=your_key_here
   ```
   Add that line to your `~/.zshrc` or `~/.bashrc` so it's always there.
   See `SETUP.html` for the friendly step-by-step.
3. Put the exam papers in place (see below), then run a script, e.g.:
   ```bash
   python scripts/pipeline.py --topic "Heat" --bank p4
   ```

> 🔑 **No `.env` file.** The tool reads your API key straight from your computer's
> environment, so the secret key is never stored inside the project and can't be
> pushed to GitHub by accident.

## Exam papers (kept private 🔒)

The past-year exam PDFs are **copyrighted**, so they are **not** in this public repo.
They live in my private `science-banks` repo. To run the tool, put that repo's
`banks/` folder (and `syllabus.pdf`) inside this folder so the scripts can find them.

## Built by Claire — Primary 5
