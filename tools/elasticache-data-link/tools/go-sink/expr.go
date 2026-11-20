package main

// The transform engine's numbers and computed-field expressions (Jira story F077).
//
// WHAT WAS WRONG
// The sink and the bulk loader each had their own: the sink a lenient
// hand-written parser, the loader Python's own expression parser and float().
// They disagreed. `1e3` was 1 in the sink and 1000 in the loader; `a % b` was
// a in the sink and 0 in the loader; NaN printed "NaN" and "nan"; a field
// value "1e400" was 0 and inf, "0x1p3" 8 and 0, "١٢" 0 and 12. A row loaded
// and the same row written by CDC got different computed values, and a
// mistyped expression silently computed something.
//
// THE RULES, identical in data-plane/bulk-loader/transforms.py and checked by
// both test suites against testdata/transform_cases.json:
//
//	expr    := term (('+' | '-') term)*
//	term    := unary (('*' | '/') unary)*
//	unary   := ('+' | '-') unary | primary
//	primary := number | name | '(' expr ')'
//	number  := (digits ['.' [digits]] | '.' digits) [('e' | 'E') ['+' | '-'] digits]
//	name    := [A-Za-z_][A-Za-z0-9_]*
//
// Spaces, tabs and newlines may separate tokens. Anything else (%, **, a
// function call, two operands in a row, an unbalanced parenthesis) is an
// error, and a spec with such an expression is refused when it is loaded.
// A name reads that field of the source row as a number; a field that is not
// one, or is missing, reads as 0. Division by zero gives 0. Arithmetic is
// IEEE double, so an overflow gives Infinity.
//
// A field value is a number when, after trimming ASCII whitespace, it is a
// plain decimal ([+-]digits[.digits][e[+-]digits]) or NaN / Inf / Infinity in
// any case with an optional sign. Results print as %.6f with trailing zeros
// and the dot dropped, -0 as 0, and NaN / Infinity / -Infinity the way Java,
// and so Debezium, prints a FLOAT or DOUBLE column.

import (
	"fmt"
	"math"
	"strconv"
	"strings"
)

// asciiSpace is what a number may be padded with. Unicode spaces are not
// trimmed: Python's strip() and Go's TrimSpace disagree on which they are.
const asciiSpace = " \t\n\r\f\v"

// scanDecimal returns the length of the decimal number at the start of s, or
// 0 when there is none. The exponent is taken only when digits follow it.
func scanDecimal(s string) int {
	digits := func(i int) int {
		j := i
		for j < len(s) && s[j] >= '0' && s[j] <= '9' {
			j++
		}
		return j
	}
	i := digits(0)
	intPart := i > 0
	if i < len(s) && s[i] == '.' {
		j := digits(i + 1)
		if !intPart && j == i+1 {
			return 0 // "." alone
		}
		i = j
	} else if !intPart {
		return 0
	}
	if i < len(s) && (s[i] == 'e' || s[i] == 'E') {
		j := i + 1
		if j < len(s) && (s[j] == '+' || s[j] == '-') {
			j++
		}
		if k := digits(j); k > j {
			i = k
		}
	}
	return i
}

// decimalValue is the nearest double to a decimal scanDecimal accepted. An
// out-of-range value is ±Inf (or 0), as Python's float() gives it.
func decimalValue(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}

// parseNumber reads a field value as a number (see the rules above).
func parseNumber(s string) (float64, bool) {
	s = strings.Trim(s, asciiSpace)
	body, sign := s, 1.0
	if body != "" && (body[0] == '+' || body[0] == '-') {
		if body[0] == '-' {
			sign = -1
		}
		body = body[1:]
	}
	switch strings.ToLower(body) {
	case "nan":
		return math.NaN(), true
	case "inf", "infinity":
		return math.Inf(int(sign)), true
	}
	if body == "" || scanDecimal(body) != len(body) {
		return 0, false
	}
	return sign * decimalValue(body), true
}

// toFloat is parseNumber with 0 for anything that is not a number.
func toFloat(s string) float64 {
	f, _ := parseNumber(s)
	return f
}

// formatNum prints a computed value (see the rules above).
func formatNum(v float64) string {
	switch {
	case math.IsNaN(v):
		return "NaN"
	case math.IsInf(v, 1):
		return "Infinity"
	case math.IsInf(v, -1):
		return "-Infinity"
	}
	s := strconv.FormatFloat(v, 'f', 6, 64)
	s = strings.TrimRight(s, "0")
	s = strings.TrimSuffix(s, ".")
	if s == "-0" {
		s = "0"
	}
	return s
}

// maxExprDepth bounds nested parentheses and signs, so a pathological
// expression is refused the same way by every engine (Python would otherwise
// hit its recursion limit).
const maxExprDepth = 64

// exprParser parses and, with a row, evaluates one expression.
type exprParser struct {
	s     string
	pos   int
	row   map[string]string
	depth int
}

func (p *exprParser) enter() error {
	p.depth++
	if p.depth > maxExprDepth {
		return p.fail(fmt.Sprintf("nested more than %d deep", maxExprDepth))
	}
	return nil
}

func (p *exprParser) skipSpace() {
	for p.pos < len(p.s) && strings.IndexByte(" \t\n\r", p.s[p.pos]) >= 0 {
		p.pos++
	}
}

func (p *exprParser) fail(what string) error {
	if p.pos >= len(p.s) {
		return fmt.Errorf("%s at the end of %q", what, p.s)
	}
	return fmt.Errorf("%s at position %d of %q (%q)", what, p.pos+1, p.s, p.s[p.pos:p.pos+1])
}

func (p *exprParser) expr() (float64, error) {
	v, err := p.term()
	for err == nil {
		p.skipSpace()
		if p.pos >= len(p.s) || (p.s[p.pos] != '+' && p.s[p.pos] != '-') {
			break
		}
		op := p.s[p.pos]
		p.pos++
		var r float64
		if r, err = p.term(); err == nil {
			if op == '+' {
				v += r
			} else {
				v -= r
			}
		}
	}
	return v, err
}

func (p *exprParser) term() (float64, error) {
	v, err := p.unary()
	for err == nil {
		p.skipSpace()
		if p.pos >= len(p.s) || (p.s[p.pos] != '*' && p.s[p.pos] != '/') {
			break
		}
		op := p.s[p.pos]
		p.pos++
		var r float64
		if r, err = p.unary(); err == nil {
			switch {
			case op == '*':
				v *= r
			case r == 0:
				v = 0
			default:
				v /= r
			}
		}
	}
	return v, err
}

func (p *exprParser) unary() (float64, error) {
	p.skipSpace()
	if p.pos < len(p.s) && (p.s[p.pos] == '+' || p.s[p.pos] == '-') {
		neg := p.s[p.pos] == '-'
		p.pos++
		if err := p.enter(); err != nil {
			return 0, err
		}
		v, err := p.unary()
		p.depth--
		if neg {
			v = -v
		}
		return v, err
	}
	return p.primary()
}

func isNameStart(c byte) bool { return c == '_' || (c|0x20 >= 'a' && c|0x20 <= 'z') }
func isNameByte(c byte) bool  { return isNameStart(c) || (c >= '0' && c <= '9') }

func (p *exprParser) primary() (float64, error) {
	p.skipSpace()
	if p.pos >= len(p.s) {
		return 0, p.fail("a number, a column name or '(' expected")
	}
	c := p.s[p.pos]
	switch {
	case c == '(':
		p.pos++
		if err := p.enter(); err != nil {
			return 0, err
		}
		v, err := p.expr()
		p.depth--
		if err != nil {
			return 0, err
		}
		p.skipSpace()
		if p.pos >= len(p.s) || p.s[p.pos] != ')' {
			return 0, p.fail("')' expected")
		}
		p.pos++
		return v, nil
	case (c >= '0' && c <= '9') || c == '.':
		n := scanDecimal(p.s[p.pos:])
		if n == 0 {
			return 0, p.fail("a number expected")
		}
		v := decimalValue(p.s[p.pos : p.pos+n])
		p.pos += n
		return v, nil
	case isNameStart(c):
		start := p.pos
		for p.pos < len(p.s) && isNameByte(p.s[p.pos]) {
			p.pos++
		}
		return toFloat(p.row[p.s[start:p.pos]]), nil
	}
	return 0, p.fail("a number, a column name or '(' expected")
}

// parseExpr evaluates expr over row, or says why expr is not valid.
func parseExpr(expr string, row map[string]string) (float64, error) {
	p := &exprParser{s: expr, row: row}
	v, err := p.expr()
	if err != nil {
		return 0, err
	}
	p.skipSpace()
	if p.pos < len(p.s) {
		return 0, p.fail("an operator expected")
	}
	return v, nil
}

// checkExpr reports why an expression would be refused, or nil.
func checkExpr(expr string) error {
	_, err := parseExpr(expr, nil)
	return err
}

// evalExpr evaluates a computed field. A spec is checked when it is loaded,
// so an invalid expression only reaches here from a spec built in code; it
// computes 0.
func evalExpr(expr string, row map[string]string) float64 {
	v, err := parseExpr(expr, row)
	if err != nil {
		return 0
	}
	return v
}

// filterOps are the filter comparisons. An unknown one used to mean "keep
// every row"; it is now refused when the spec is loaded.
var filterOps = map[string]bool{"=": true, "!=": true, ">": true, ">=": true, "<": true, "<=": true}

// checkSpec refuses a spec that the two engines would not apply the same way.
func checkSpec(s *TransformSpec) error {
	for table, ts := range s.Tables {
		if f := ts.Filter; f != nil {
			if f.Field == "" {
				return fmt.Errorf("transforms.tables.%s.filter: field is required", table)
			}
			if !filterOps[f.Op] {
				return fmt.Errorf("transforms.tables.%s.filter: op %q is not one of = != > >= < <=", table, f.Op)
			}
		}
		for i, cf := range ts.Computed {
			if cf.Name == "" {
				return fmt.Errorf("transforms.tables.%s.computed[%d]: name is required", table, i)
			}
			if err := checkExpr(cf.Expr); err != nil {
				return fmt.Errorf("transforms.tables.%s.computed[%d] (%s): %w", table, i, cf.Name, err)
			}
		}
		to := map[string]string{}
		for src, dst := range ts.Rename {
			if other, ok := to[dst]; ok {
				a, b := other, src
				if b < a {
					a, b = b, a
				}
				return fmt.Errorf("transforms.tables.%s.rename: %s and %s are both renamed to %s", table, a, b, dst)
			}
			to[dst] = src
		}
	}
	return nil
}
