"""Graph analysis for cyclic routing and acyclic dependency barriers."""

from __future__ import annotations


def predecessors(workflow):
    graph = {node["id"]: set() for node in workflow["nodes"]}
    for edge in workflow["edges"]:
        graph[edge["to"]].add(edge["from"])
    return graph


def ancestors(graph, node_id):
    seen, pending = set(), list(graph[node_id])
    while pending:
        node = pending.pop()
        if node not in seen:
            seen.add(node)
            pending.extend(graph[node])
    return seen - {node_id}


def analyse(workflow):
    """Remove DFS feedback edges to form a DAG, without changing the spec.

    Iterative DFS avoids Python recursion limits. Roots and outgoing edge order
    come from the authored graph; feedback edges activate by completion sequence,
    while forward edges retain the backend's existing all-parent join barrier.
    """
    graph = predecessors(workflow)
    roots = [name for name, parents in graph.items() if not parents]
    outgoing = {name: [] for name in graph}
    for edge in workflow["edges"]:
        outgoing[edge["from"]].append(edge["to"])
    colours, feedback = {}, set()
    for start in roots + list(graph):
        if start in colours:
            continue
        colours[start] = 1
        stack = [(start, iter(outgoing[start]))]
        while stack:
            source, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                colours[source] = 2
                stack.pop()
            elif colours.get(target) == 1:
                feedback.add((source, target))
            elif target not in colours:
                colours[target] = 1
                stack.append((target, iter(outgoing[target])))
    forward = {name: set(parents) for name, parents in graph.items()}
    for source, target in feedback:
        forward[target].discard(source)
    reachable = set()
    pending = roots[:1]
    while pending:
        name = pending.pop()
        if name not in reachable:
            reachable.add(name)
            pending.extend(outgoing[name])
    return graph, forward, feedback, roots, reachable
