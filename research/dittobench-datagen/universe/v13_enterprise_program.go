package universe

import (
	"fmt"
	"sort"
	"strconv"
)

// Programs compose relation traversal, evidence-based filtering and a terminal
// projection/aggregation. A missing field is an invalid task, not a false match.
type V13EnterpriseStep struct{ Op, Field, Value string }

func EvaluateV13EnterpriseProgram(w V13EnterpriseWorld, entity string, at int, steps []V13EnterpriseStep) ([]string, error) {
	if len(steps) == 0 || len(steps) > 16 {
		return nil, fmt.Errorf("enterprise program: invalid length")
	}
	s, err := w.state(at)
	if err != nil {
		return nil, err
	}
	if s[entity] == nil {
		return nil, fmt.Errorf("enterprise program: missing entity")
	}
	// Validate the program even if a legitimate filter selects no entities.
	for i, step := range steps {
		terminal := step.Op == "project" || step.Op == "sum" || step.Op == "count"
		if terminal != (i == len(steps)-1) {
			return nil, fmt.Errorf("enterprise program: terminal position")
		}
		switch step.Op {
		case "follow":
			if (!enterpriseReferenceFields[step.Field] && !enterpriseSetFields[step.Field]) || step.Value != "" {
				return nil, fmt.Errorf("enterprise program: invalid reference")
			}
		case "filter":
			if step.Field == "" || step.Value == "" {
				return nil, fmt.Errorf("enterprise program: invalid filter")
			}
		case "project":
			if step.Field == "" || step.Value != "" {
				return nil, fmt.Errorf("enterprise program: invalid projection")
			}
		case "sum":
			if step.Field != "allocation_hours" || step.Value != "" {
				return nil, fmt.Errorf("enterprise program: untyped sum")
			}
		case "count":
			if step.Field != "" || step.Value != "" {
				return nil, fmt.Errorf("enterprise program: invalid count")
			}
		default:
			return nil, fmt.Errorf("enterprise program: unknown operation")
		}
	}
	entities := []string{entity}
	for _, step := range steps {
		if step.Op == "count" {
			return []string{strconv.Itoa(len(entities))}, nil
		}
		next := map[string]bool{}
		var projected []string
		var sum int64
		for _, id := range entities {
			values, ok := s[id][step.Field]
			if !ok {
				return nil, fmt.Errorf("enterprise program: missing evidence")
			}
			switch step.Op {
			case "follow":
				for _, v := range values {
					if s[v] == nil {
						return nil, fmt.Errorf("enterprise program: dangling reference")
					}
					next[v] = true
				}
			case "filter":
				if len(values) != 1 {
					return nil, fmt.Errorf("enterprise program: nonscalar filter")
				}
				if values[0] == step.Value {
					next[id] = true
				}
			case "project":
				projected = append(projected, values...)
			case "sum":
				if len(values) != 1 {
					return nil, fmt.Errorf("enterprise program: nonscalar amount")
				}
				n, err := strconv.ParseInt(values[0], 10, 64)
				if err != nil || n < 0 || n > 1000000 {
					return nil, fmt.Errorf("enterprise program: invalid hours")
				}
				sum += n // <= 10000 entities * 1000000; safely inside int64.
			}
		}
		if step.Op == "project" {
			sort.Strings(projected)
			return projected, nil
		}
		if step.Op == "sum" {
			return []string{strconv.FormatInt(sum, 10)}, nil
		}
		entities = nil
		for id := range next {
			entities = append(entities, id)
		}
		sort.Strings(entities)
	}
	return nil, fmt.Errorf("enterprise program: missing result")
}
