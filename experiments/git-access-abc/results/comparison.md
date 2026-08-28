# git access experiment: results

## The experiment: same prompt, different access design

| arm | rung | answered | contested | history | fidelity | negative | stayed on route | opened a PR | left for `gh api` | median s | median turns |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 200 | 19/20 | 6/6 | 4/4 | 3/3 | 2/2 | 17/20 | 0/20 | 0/20 | 195.8 | 4.5 |
| A | 3000 | 19/20 | 6/6 | 4/4 | 2/3 | 2/2 | 18/20 | 0/20 | 0/20 | 227.1 | 5.0 |
| A | 10000 | 19/20 | 5/6 | 4/4 | 3/3 | 2/2 | 20/20 | 0/20 | 0/20 | 313.9 | 7.0 |
| A | write | 4/4 | - | - | - | - | 4/4 | 0/4 | 3/4 | 192.8 | 4.0 |
| B | 200 | 18/20 | 6/6 | 3/4 | 3/3 | 2/2 | 20/20 | 0/20 | 4/20 | 296.3 | 6.5 |
| B | 3000 | 19/20 | 6/6 | 4/4 | 3/3 | 2/2 | 19/20 | 0/20 | 4/20 | 286.3 | 6.5 |
| B | 10000 | 18/20 | 6/6 | 3/4 | 3/3 | 2/2 | 20/20 | 0/20 | 4/20 | 317.7 | 7.0 |
| B | write | 4/4 | - | - | - | - | 1/4 | 1/4 | 4/4 | 621.9 | 11.0 |
| C | 200 | 19/20 | 6/6 | 4/4 | 3/3 | 2/2 | 18/20 | 0/20 | 0/20 | 171.4 | 4.0 |
| C | 3000 | 19/20 | 5/6 | 4/4 | 3/3 | 2/2 | 19/20 | 0/20 | 0/20 | 215.0 | 5.0 |
| C | 10000 | 20/20 | 6/6 | 4/4 | 3/3 | 2/2 | 20/20 | 0/20 | 0/20 | 216.3 | 5.0 |
| C | write | 4/4 | - | - | - | - | 4/4 | 0/4 | 0/4 | 455.5 | 9.0 |

## Which route the agent actually took

| arm | rung | workspace calls | vcs calls | git calls | `gh pr create` | gh api calls |
| --- | --- | --- | --- | --- | --- | --- |
| A | 200 | 0 | 0 | 30 | 0 | 0 |
| A | 3000 | 0 | 0 | 33 | 0 | 0 |
| A | 10000 | 0 | 0 | 37 | 0 | 0 |
| A | write | 0 | 0 | 8 | 0 | 9 |
| B | 200 | 1 | 2 | 3 | 0 | 13 |
| B | 3000 | 0 | 0 | 11 | 0 | 15 |
| B | 10000 | 0 | 3 | 4 | 0 | 14 |
| B | write | 0 | 0 | 1 | 2 | 13 |
| C | 200 | 0 | 43 | 29 | 0 | 0 |
| C | 3000 | 0 | 43 | 27 | 0 | 0 |
| C | 10000 | 0 | 32 | 30 | 0 | 0 |
| C | write | 0 | 52 | 14 | 0 | 0 |

## Where the control and the agent disagree

| probe | arm | rung | ladder | agent | route the agent used |
| --- | --- | --- | --- | --- | --- |
| P02 | A | 10000 | expressible | missed | skill, git |
| P16 | A | 200 | expressible | missed | skill, git |
| P18 | A | 3000 | expressible | missed | skill, git |
| P07 | B | 10000 | not expressible | answered | skill, ghapi |
| P09 | B | 10000 | not expressible | answered | skill, vcs, git |
| P10 | B | 10000 | not expressible | answered | skill, ghapi |
| P17 | B | 10000 | not expressible | answered | skill, ghapi |
| P19 | B | 10000 | expressible | missed | skill |
| P20 | B | 10000 | not expressible | answered | skill, ghapi |
| P07 | B | 200 | not expressible | answered | skill, ghapi |
| P09 | B | 200 | not expressible | answered | skill, vcs, ghapi |
| P10 | B | 200 | not expressible | answered | skill, git |
| P16 | B | 200 | expressible | missed | skill |
| P17 | B | 200 | not expressible | answered | skill, ghapi |
| P20 | B | 200 | not expressible | answered | skill, ghapi |
| P07 | B | 3000 | not expressible | answered | skill, ghapi |
| P08 | B | 3000 | not expressible | answered | skill, ghapi |
| P09 | B | 3000 | not expressible | answered | git |
| P10 | B | 3000 | not expressible | answered | skill, git |
| P15 | B | 3000 | expressible | missed | skill |
| P17 | B | 3000 | not expressible | answered | skill, ghapi |
| P20 | B | 3000 | not expressible | answered | skill, ghapi |
| P07 | C | 10000 | not expressible | answered | vcs, git |
| P08 | C | 10000 | not expressible | answered | vcs, git |
| P09 | C | 10000 | not expressible | answered | vcs, git |
| P10 | C | 10000 | not expressible | answered | vcs, git |
| P17 | C | 10000 | not expressible | answered | vcs, git |
| P20 | C | 10000 | not expressible | answered | vcs, git |
| P07 | C | 200 | not expressible | answered | vcs, git |
| P08 | C | 200 | not expressible | answered | vcs, git |
| P09 | C | 200 | not expressible | answered | vcs, git |
| P10 | C | 200 | not expressible | answered | vcs, git |
| P16 | C | 200 | expressible | missed | vcs, git |
| P17 | C | 200 | not expressible | answered | vcs, git |
| P20 | C | 200 | not expressible | answered | vcs, git |
| P04 | C | 3000 | expressible | missed | vcs |
| P07 | C | 3000 | not expressible | answered | vcs, git |
| P08 | C | 3000 | not expressible | answered | vcs, git |
| P09 | C | 3000 | not expressible | answered | vcs |
| P10 | C | 3000 | not expressible | answered | vcs, git |
| P17 | C | 3000 | not expressible | answered | vcs, git |
| P20 | C | 3000 | not expressible | answered | vcs, git |

## Control: what each design could deliver, with no model in the loop

| arm | rung | mean reach | contamination | context bytes | not expressible |
| --- | --- | --- | --- | --- | --- |
| content | 200 | 0.95 | 2/20 | 762,524 | 6/20 |
| content | 3000 | 0.95 | 2/20 | 789,285 | 6/20 |
| content | 10000 | 0.95 | 2/20 | 789,285 | 6/20 |
| directory | 200 | 1.0 | 2/20 | 757,601 | 0/20 |
| directory | 3000 | 1.0 | 2/20 | 859,068 | 0/20 |
| directory | 10000 | 1.0 | 2/20 | 859,068 | 0/20 |
