GRAPH ENGINEERING · A NODE IS A JOB · AN EDGE IS A HANDOFF · A LOOP IS ALREADY A GRAPH · A CHECK AT EVERY HANDOFF · SAY IT · FILE IT · DRAW IT · CODE IT · GLITCH CAT CLUB ·

Glitch Cat Club · field notes · part three

# Graph *engineering*

The goal

Know when a task needs a chat, loop or graph.

All three built on the same research task.

What you learn, in eight cards

01 · how we got here

## Graph engineering began as a joke.

The joke

Peter Steinberger

@steipete · made OpenClaw

Are we still talking loops or did we shift to graphs yet?

17 July 2026 · 21:34

4.5 hours later

Loop engineering is dead. Enter graph engineering.

Hamel Husain, an article on X. Santiago Valdarrama passed it on the same day.

**48 hours later**Three competing definitions of graph engineering and a wave of copycat posts. Millions of views.

**By their own account**Both posts were jokes. The word stuck anyway, because it pointed to something honest.

The names before it

Five names in two years. Each one is a thing you learned to control. The first three make one answer better. The last two change how the work runs.

**Prompt engineering**What you say to it. The first thing anyone could control, so everyone learned to say it well.

**Context engineering**What it knows. Put the right material in front of the model and the output improves before you touch anything else.

**Harness engineering**Where it runs and what it can touch. The tools and the rules around the model decide as much of the quality as the model does.

**Loop engineering**How long it runs on its own. A method, not an input: the work keeps going until a finish line is met. Named in June, after Boris Cherny, who leads Claude Code, said he no longer prompts Claude, he writes the loops that do.

**Graph engineering**How many run together. Several agents on one job, each with one role. A check where the work changes hands. Named in July, in the joke above.

**Conclusion**Every one of these names marks another part of the work moving from you to the system. The first three decide what one answer is made from. Loop and graph decide how the work runs: for how long and across how many. Neither is new. A loop is the oldest control structure in programming. A graph of steps is how build tools and workflow engines have run jobs for decades. What is new is the model inside them.

02 · the word graph

## What does graph mean?

Say graph engineering to someone in AI and they usually presume you mean a knowledge graph. The word has several definitions.

The shape

A graph is nodes joined by edges. Directed when the edges point. A chart of numbers is also called a graph. That is a different sense of the same word.

Three meanings in AI

The shape is the same every time. What differs is what a node stands for.

### A node is a thing

People, companies, places. An edge is how two of them relate. This is a **knowledge graph**: what the AI knows. You put facts in and ask questions of it. Every tool that stores, builds or draws one is this kind.

### A node is a cause

Events. Every edge means causes. This is a **causal graph**: what leads to what. Used to reason about why an outcome happened or what would change it.

### A node is a step

Steps of work. An edge says which runs next. This is a **workflow graph**: what the AI does. You need it once steps depend on each other: this one waits for that one, two run at the same time, a failed check goes back. LangGraph is a code library for writing one.

**Conclusion**Graph engineering uses the third definition: a workflow graph with an agent at every node and a check at every handoff. An agent is one AI session given one job. A loop is any path that returns to where it started. So the loop on the next card is already a graph, with one job in it.

03 · loops

## A graph is made of loops.

Graph engineering extends loop engineering. Each node is a loop. Each edge is where one loop hands to the next.

What a loop is

One agent running turn after turn until a finish line is met. Your tool already runs a small one inside every prompt. The loop you control adds the finish line, written before the work starts. A loop is not a schedule. A schedule decides when work starts. A loop decides when it stops.

What a loop needs

Loop only when one pass cannot finish the job and you can check whether it is done.

**A finish line you can check**"At least 30 tools. Every row has a URL, a price, local-first yes or no, an export format and the date checked." Counts and facts, so a check can say yes or no. A word like insightful cannot be checked, so a loop built on it never knows when to stop.

**A safe draft to work in**"A draft file only. Nothing published." The loop runs unattended for up to thirty turns, so it works on something it can get wrong: a draft file. Never a live document, a sent email or a published post.

**A task that gets closer each turn**"Every turn, say the turn number, what you did and the row count." Each turn adds rows, so the count moves towards the finish line and you can watch it move. A job with nothing to count, like make it better, has no direction and never ends.

Where loops go wrong

**It keeps spending**The finish line asks for something that does not exist, so no turn ever meets it. Solution: a cap inside the finish line. Example: "Stop after 30 turns regardless."

**It goes round without progress**The count stops moving because the sources are exhausted or the check is too loose. Solution: a log line, so you see it early. Example: "Every turn, say the turn number, what you did and the row count." Same count three turns running: stop it and ask what it tried.

**It finishes and the result is mediocre**It met the letter of the finish line, which counted rows and said nothing about quality. Solution: write the quality into the finish line before you run. Example: "Every row has a URL" and "at least 10 open source, at least 10 paid". A row count alone would have passed a list of names.

**It should never have been a loop**One pass would have done it and you paid for thirty. Or the answer is your own judgement, so nothing counts it: who to hire, what to say to your team. Solution: the test at the top of this card. Both must hold: one pass cannot finish it and a check can say it is done. Otherwise it is a chat.

Practical exercise

**Objective**A sourced scan of AI memory and second-brain tools, September 2026.

**Output**`memory-tools-scan.md`: a three-line summary, then one table, one row per tool.

**Done when**At least 30 tools. Every row has a URL, a price, local-first yes or no, an export format and the date checked. At least 10 open source, at least 10 paid and at least 5 plug-ins. No two rows name the same tool.

**Log**Every turn, say the turn number, what you did and the row count.

**Stages**Sweep, check every URL, remove duplicates, write the table.

**Stop caps**Stop after 30 turns regardless. A draft file only. Nothing published.

the loop · unattended

```
> /goal memory-tools-scan.md is done when all of the following are true. The artefact: a three-line summary, then one table, one row per tool. The quota: at least 30 AI memory or second-brain tools. No two rows name the same tool. The receipts: every row has a URL, a price, local-first yes or no, an export format and the date checked. The mix: at least 10 open source, at least 10 paid and at least 5 plug-ins. The log: every turn, say the turn number, what you did and the row count. Stop after 30 turns regardless. A draft file only. Nothing published.
◎ /goal active
Kem's run goes here
turn 2 · not yet met · the small model's reason prints here
turn 3 · not yet met · …
turn N · met · goal achieved
```

**Conclusion**A loop is one agent, one job, one finish line. That is one node. In coding the finish line comes free: tests pass or fail. In knowledge work you write it yourself. Every failure above traces back to how well you wrote it. The scan on the goal card is the use case from here on.

04 · why knowledge work is hard to check

## Code can be checked by a machine. Knowledge work has to be checked by a referee you build.

### What knowledge work is

Peter Drucker’s term for work whose product is a judgement: a report, a plan, a scan, a decision. Research is the everyday case. Nothing compiles it.

### Why it is hard to check

A test on code gives the same pass or fail every time. A research answer comes out different on every run. No machine says which one is good enough. So you write the finish line yourself, as things that can be counted.

Code against knowledge work

Left: code checks code. Tests pass or fail, for free. Right: a rubric and a second agent that has not seen the work. The rubric is the list of things you will count, written before the work starts. The second agent reads the result against it.

Three ways to check the same finish line

The goal command on card 03 is one of three ways to read the same finish line. Stricter to the right.

**1 The sentence**Write the finish line into the prompt: "keep going until memory-tools-scan.md has 30 rows with a URL each; say the row count every turn". Works in any tool, with or without a goal command.

**2 A judge model**`/goal` in Claude Code or Codex. In Claude Code a separate small model reads the transcript after every turn and says met or not yet. Card 03 ran this.

**3 A script**A Stop hook: a small script that runs every time the model tries to stop, counts the rows, opens the links and refuses the stop until they pass. Deterministic: the same file gets the same verdict every time.

**Conclusion**Knowledge work has no referee until you write one. The referee is a list of countable things, written before the work, read by something that did not do the work. The three forms above differ only in how strict that reader is.

05 · when a loop stops being enough

## The first graph is a loop plus one checker.

The loop on card 03 checks itself. Its finish line counts rows and receipts. Nothing in it opens a link. A table of dead links passes. That blind spot is the gap a graph closes.

Three stops. One agent once. One agent in a loop. Several agents on one job, with a checker that never saw the work. Most jobs never leave the first two.

What you add first

Keep the loop. Add one second agent that opens the links, given only the file and the finish line, never the conversation that made the file. Models agree with themselves, so the check has to come from a fresh agent. Two nodes, one handoff, a check on it. That is a graph.

When to make the move

A loop is one agent inside one context window. A graph splits the job across more than one. You split only because one context has two limits.

**It cannot check itself**A session cannot un-know what it did, so it says done and you keep finding what it should have caught. The check has to come from a context that never saw the work: a checker on a fresh agent or a different model. The only graph most jobs ever need.

**It cannot be two things**The careful researcher starts writing creatively too early. The finish line keeps changing because the card hides two jobs. Three sources are read one after the other. More turns change nothing. One job per agent, the file handed between them. Run at once what can run at once.

**Neither is happening**Stay in the loop.

If this looks familiar

Your tool already does this when it spawns sub-agents on a big task. That is a graph too, improvised by the model as it runs: which agents, what each one sees and where the check sits are its choices. Its checker usually gets everything it knows. Graph engineering is the same machinery drawn first, by you. Decisions live on the edges: fail goes back, pass comes to you. Anything a decision tree can say, a graph can hold.

Left: the model improvises the graph and feeds its checker its own context. Right: the same job drawn first. The checker gets the file only. The dashed edge is the decision: fail goes back.

**Conclusion**Use a chat when one pass can do the job and you will read the answer yourself. Use a loop when one pass cannot and you can count the finish line. Use a graph when the check must come from outside the work or one agent is being asked to be two. Every extra agent costs tokens and attention, so start with one checker and split only when it has to be split. A graph is the sub-agent pattern you have already seen, drawn before it runs. Simpler than the name suggests.

06 · the scan as a graph

## The scan, run as a graph.

Three collectors at once, one merge, a checker that never saw their work, then you. A sub-agent is an agent that another agent starts, with its own fresh context. Drawn first, then typed as one sentence, then filed so it runs again by name.

Draw it

Draw it on a whiteboard before anything runs. Until you can draw who does what and what passes between them, there is nothing to automate. Then give the tool a photo of it with the sentence below.

Here is a photo of my drawing. Build exactly this: the pink boxes are agents, the dashed box is a checker that must not see how the work was made, the pill is me.

Say it

Scan AI memory tools with three sub-agents in parallel: one for open source, one for paid tools, one for plug-ins. Merge their rows into memory-tools-scan.md. Then hand only the file path and rubric.md to a fresh sub-agent that opens ten of the links and checks the count and the mix. It must not see how the file was made.

the graph run

```
> Scan AI memory tools with three sub-agents in parallel: one for open source, one for paid tools, one for plug-ins. Merge their rows into memory-tools-scan.md. Then hand only the file path and rubric.md to a fresh sub-agent that opens ten of the links and checks the count and the mix. It must not see how the file was made.
◎ three sub-agents running at once
  open-source   12 rows · 13 links opened · 1 dropped
  paid-tools    12 rows · 23 links opened · 4 dropped
  plug-ins      12 rows · 12 links opened · 3 dropped
◎ merge → memory-tools-scan.md
  3 duplicates removed: Mem0, Letta, Basic Memory · 33 rows · 12 open source
◎ checker, fresh context: given the file path and rubric.md, nothing else
  rows: 33 · open source: 12 · duplicates: none
  links opened: 10 · live: 10 · dead: 0
  verdict: PASS on all four rubric items
abridged: the agents' tables and the checker's full report are in the run folder
```

The loop’s finish line could count rows. Only the checker opened links: ten of them, read against the rubric, with no memory of how the file was made. A dead link would have failed the run here. This one had none.

The handover

The session starts the checker with a prompt holding two file paths: the merged table and rubric.md, the countable finish line from card 04. The checker begins with an empty context, reads both files from disk, opens ten links and returns pass or fail. It cannot see how the table was made, because that conversation was never given to it.

The same request as a plain chat

one pass, no tools

```
> List AI memory and second-brain tools with their prices.
Here's a rundown:

- Mem: free tier; Pro around $10-15/month
- Notion AI: $8-10/month add-on to a Notion plan
- Obsidian: free; Sync $8/month, Publish $16/month
- Reflect Notes: $10/month or $99/year
- Tana: free tier; paid plans roughly $12-20/month
- RemNote: free tier; Pro around $6-8/month
- Roam Research: $15/month or $165/year
- mymind: around $10-14/month
- Personal AI: free tier; paid plans from $10-40/month
- Recall (getrecall.ai): free tier; Pro around $10-15/month
- Saner.AI: subscription around $12-20/month
- Rewind / Limitless: $19/month, plus hardware cost for the Limitless pendant

Prices shift often and vary by region or promo, so treat these as ballpark figures rather than current quotes.
```

Twelve names, hedged prices, no links. That is what one pass gives you. For many questions it is enough.

File it, so it runs again by name

.claude/agents/checker.md

--- name: checker description: Checks a finished file against rubric.md. Never sees how the file was made. model: sonnet maxTurns: 15 --- You receive a file path and a rubric. Read both. Pick ten rows at random and open each URL. Count the rows, the open-source rows and any duplicates. Report pass or fail against the rubric, with the failing rows listed. Do not fix anything.

.claude/skills/scan/SKILL.md

--- description: The scan as a graph of agents. Run with /scan \<topic>. --- Phase 1: run open-source, paid-tools and plug-ins at once, each with the topic. Phase 2: merge their rows into memory-tools-scan.md. Remove duplicates. Phase 3: hand the file path and rubric.md to checker. Nothing else. Fail: back to phase 1 with the failing rows. Pass: show me the table.

1. **Create the two files above**, plus one agent file per collector with the same shape. An agent file defines one agent. A skill file defines the steps that run when you type the command. Its last line is a decision on an edge: fail goes back to phase 1, pass comes to you.
2. **Type `/scan AI memory tools`.** The skill runs the phases and calls each agent by name. Phase 3 is the handover above.
3. **To call one agent on its own**, type `@`, pick *checker (agent)*, then say what to check.

**Conclusion**One graph, written three ways. The drawing is what you agree. The sentence runs it once. The files run it again by name.

07 · the same graph as code

## A graph is a workflow.

Once it is drawn, it can be written four ways.

1. **Draw it.** The whiteboard. What you agree before anything runs.
2. **Say it.** One sentence, as on card 06. Runs it once.
3. **File it.** Agent and skill files, so it runs again by name. A skill can call scripts too.
4. **Code it.** For when it runs with no chat window open: on a schedule or inside a product. LangGraph is one free tool for it.

Our scan, in code

The same nodes as the whiteboard on card 06. Every box is code.

The file

Our scan as a LangGraph file. 35 lines, run here today.

graph_min.py · langgraph 1.2.11

import operator from typing import Annotated, TypedDict from langgraph.graph import StateGraph, START, END class State(TypedDict): rows: Annotated\[list\[str\], operator.add\] # every collector adds to one shared list turn: int def collector(name): # one function shape, three collectors def run(s): return {"rows": \[f"{name} row {s\['turn'\] + 1}", f"{name} row {s\['turn'\] + 2}"\]} return run def merge(s): # the deterministic step, no model return {"turn": s\["turn"\] + 1} def checker(s): # the referee: count against the bar print(f"turn {s\['turn'\]}: {len(s\['rows'\])} rows") return {} def route(s): # the edge back: under 12 rows, go again return \["open_source", "paid_tools", "plug_ins"\] if len(s\["rows"\]) \< 12 else END g = StateGraph(State) for n in ("open_source", "paid_tools", "plug_ins"): g.add_node(n, collector(n)) g.add_edge(START, n) # the three start at once g.add_node("merge", merge) g.add_node("checker", checker) g.add_edge(\["open_source", "paid_tools", "plug_ins"\], "merge") # merge waits for all g.add_edge("merge", "checker") g.add_conditional_edges("checker", route, \["open_source", "paid_tools", "plug_ins", END\]) app = g.compile() app.invoke({"rows": \[\], "turn": 0}) print(app.get_graph().draw_mermaid())

python graph_min.py

```
turn 1: 6 rows
turn 2: 12 rows
```

What LangGraph drew

Left: the picture. Right: the text it printed to make it. Both came out of the file above.

the graph as LangGraph drew it

what the library printed

```
graph TD;
	__start__([<p>__start__</p>]):::first
	open_source(open_source)
	paid_tools(paid_tools)
	plug_ins(plug_ins)
	merge(merge)
	checker(checker)
	__end__([<p>__end__</p>]):::last
	__start__ --> open_source;
	__start__ --> paid_tools;
	__start__ --> plug_ins;
	checker -.-> __end__;
	checker -.-> open_source;
	checker -.-> paid_tools;
	checker -.-> plug_ins;
	merge --> checker;
	open_source --> merge;
	paid_tools --> merge;
	plug_ins --> merge;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

How it runs

**Each node**A function. Here they are placeholders, so it runs with no model.

**In practice**Each collector calls a model with its prompt. The checker calls a fresh model with only the file and the rubric.

**Start it**One command in a terminal or a schedule. No chat window open.

**Conclusion**One graph, four ways to write it. Code is the last to reach for.

08 · at scale

## At scale, the agents become reusable parts.

Chat, loop or graph: the job decides. Most work never leaves the first two.

The same agent files serve every job. The skills route each new job through them. That standing set is your team.

The goal

You can now say when a task needs a chat, loop or graph.
