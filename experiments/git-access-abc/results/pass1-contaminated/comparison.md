# git access experiment: results

## The experiment: same prompt, different access design

| arm | rung | answered | contested | history | fidelity | negative | stayed on route | opened a PR | left for `gh api` | median s | median turns |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 200 | 20/20 | 6/6 | 4/4 | 3/3 | 2/2 | 20/20 | 0/20 | 0/20 | 225.4 | 5.0 |
| A | 3000 | 9/9 | 1/1 | 2/2 | 2/2 | 1/1 | 9/9 | 0/9 | 0/9 | 173.7 | 4 |
| A | 10000 | 9/9 | 1/1 | 2/2 | 2/2 | 1/1 | 6/9 | 0/9 | 0/9 | 223.8 | 5 |
| A | write | 4/4 | - | - | - | - | 3/4 | 0/4 | 2/4 | 219.3 | 4.5 |
| B | 200 | 19/20 | 6/6 | 3/4 | 3/3 | 2/2 | 20/20 | 0/20 | 5/20 | 222.3 | 5.0 |
| B | 3000 | 9/9 | 1/1 | 2/2 | 2/2 | 1/1 | 9/9 | 0/9 | 4/9 | 425.5 | 8 |
| B | 10000 | 6/9 | 1/1 | 1/2 | 1/2 | 1/1 | 9/9 | 0/9 | 4/9 | 404.9 | 8 |
| B | write | 4/4 | - | - | - | - | 3/4 | 1/4 | 4/4 | 540.4 | 10.0 |
| C | 200 | 19/20 | 6/6 | 4/4 | 3/3 | 2/2 | 19/20 | 0/20 | 0/20 | 172.3 | 4.0 |
| C | 3000 | 20/20 | 6/6 | 4/4 | 3/3 | 2/2 | 15/20 | 0/20 | 1/20 | 216.8 | 5.0 |
| C | 10000 | 19/20 | 6/6 | 4/4 | 2/3 | 2/2 | 18/20 | 0/20 | 0/20 | 265.1 | 6.0 |
| C | write | 4/4 | - | - | - | - | 4/4 | 0/4 | 2/4 | 501.1 | 9.5 |

## Which route the agent actually took

| arm | rung | workspace calls | vcs calls | git calls | `gh pr create` | gh api calls |
| --- | --- | --- | --- | --- | --- | --- |
| A | 200 | 0 | 0 | 38 | 0 | 0 |
| A | 3000 | 0 | 0 | 19 | 0 | 0 |
| A | 10000 | 0 | 0 | 18 | 0 | 0 |
| A | write | 0 | 0 | 9 | 0 | 6 |
| B | 200 | 0 | 0 | 0 | 0 | 14 |
| B | 3000 | 0 | 0 | 0 | 0 | 12 |
| B | 10000 | 0 | 0 | 8 | 0 | 13 |
| B | write | 0 | 0 | 0 | 1 | 12 |
| C | 200 | 0 | 75 | 21 | 0 | 0 |
| C | 3000 | 0 | 50 | 17 | 0 | 1 |
| C | 10000 | 0 | 64 | 28 | 0 | 0 |
| C | write | 0 | 35 | 8 | 0 | 8 |

## Where the control and the agent disagree

| probe | arm | rung | ladder | agent | route the agent used |
| --- | --- | --- | --- | --- | --- |
| P08 | B | 10000 | not expressible | answered | skill, ghapi |
| P19 | B | 10000 | expressible | missed | skill |
| P20 | B | 10000 | not expressible | answered | skill, git, ghapi |
| P07 | B | 200 | not expressible | answered | skill, ghapi |
| P09 | B | 200 | not expressible | answered | skill, ghapi |
| P10 | B | 200 | not expressible | answered | skill, ghapi |
| P17 | B | 200 | not expressible | answered | skill, ghapi |
| P20 | B | 200 | not expressible | answered | skill, ghapi |
| P07 | B | 3000 | not expressible | answered | skill, ghapi |
| P08 | B | 3000 | not expressible | answered | skill, ghapi |
| P17 | B | 3000 | not expressible | answered | skill, ghapi |
| P20 | B | 3000 | not expressible | answered | skill, ghapi |
| P07 | C | 10000 | not expressible | answered | vcs |
| P08 | C | 10000 | not expressible | answered | vcs, git |
| P09 | C | 10000 | not expressible | answered | vcs, git |
| P10 | C | 10000 | not expressible | answered | git |
| P17 | C | 10000 | not expressible | answered | vcs, git |
| P18 | C | 10000 | expressible | missed | vcs, git |
| P20 | C | 10000 | not expressible | answered | vcs, git |
| P07 | C | 200 | not expressible | answered | vcs, git |
| P08 | C | 200 | not expressible | answered | vcs |
| P09 | C | 200 | not expressible | answered | vcs |
| P10 | C | 200 | not expressible | answered | vcs, git |
| P16 | C | 200 | expressible | missed | vcs, git |
| P17 | C | 200 | not expressible | answered | git |
| P20 | C | 200 | not expressible | answered | vcs, git |
| P07 | C | 3000 | not expressible | answered | vcs |
| P08 | C | 3000 | not expressible | answered | git |
| P09 | C | 3000 | not expressible | answered | git, ghapi |
| P10 | C | 3000 | not expressible | answered | vcs |
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
