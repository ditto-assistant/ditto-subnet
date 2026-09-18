package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"
)

// maxDepth matches MAX_DEPTH in the Python evidence tool.
const maxDepth = 16

var errNotCanonical = errors.New("canonical: bytes are not canonical JSON")

// Canonical encodes a JSON value exactly as the native-qualification Python
// tools do: json.dumps(value, sort_keys=True, separators=(",", ":")) with
// ensure_ascii. Keys sort by code point, every character outside printable
// ASCII (including U+2028 and U+2029) becomes a lowercase \uXXXX escape, and
// there is no trailing newline. Only int64 integers are accepted; floats are
// refused so both languages produce identical bytes.
func Canonical(value any) ([]byte, error) {
	var buffer bytes.Buffer
	if err := encode(&buffer, value, 0); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

// Decode strictly parses JSON: duplicate keys, non-integer numbers, trailing
// data and excessive nesting are refused. Integers decode as json.Number.
func Decode(raw []byte) (any, error) {
	if !utf8.Valid(raw) {
		return nil, errors.New("canonical: invalid UTF-8")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("canonical: trailing data")
	}
	return value, nil
}

// ParseCanonical decodes raw and requires it to be its own canonical form.
func ParseCanonical(raw []byte) (any, error) {
	value, err := Decode(raw)
	if err != nil {
		return nil, err
	}
	encoded, err := Canonical(value)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(encoded, raw) {
		return nil, errNotCanonical
	}
	return value, nil
}

func decodeValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maxDepth {
		return nil, errors.New("canonical: nested too deeply")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, fmt.Errorf("canonical: %w", err)
	}
	switch typed := token.(type) {
	case json.Delim:
		switch typed {
		case '{':
			object := map[string]any{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return nil, fmt.Errorf("canonical: %w", err)
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, errors.New("canonical: object key is not a string")
				}
				if _, duplicate := object[key]; duplicate {
					return nil, errors.New("canonical: duplicate key")
				}
				item, err := decodeValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				object[key] = item
			}
			if _, err := decoder.Token(); err != nil {
				return nil, fmt.Errorf("canonical: %w", err)
			}
			return object, nil
		case '[':
			list := []any{}
			for decoder.More() {
				item, err := decodeValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				list = append(list, item)
			}
			if _, err := decoder.Token(); err != nil {
				return nil, fmt.Errorf("canonical: %w", err)
			}
			return list, nil
		}
		return nil, errors.New("canonical: unexpected delimiter")
	case json.Number:
		if _, err := strconv.ParseInt(string(typed), 10, 64); err != nil {
			return nil, errors.New("canonical: numbers must be int64 integers")
		}
		return typed, nil
	case string, bool, nil:
		return typed, nil
	}
	return nil, errors.New("canonical: unexpected token")
}

func encode(buffer *bytes.Buffer, value any, depth int) error {
	if depth > maxDepth {
		return errors.New("canonical: nested too deeply")
	}
	switch typed := value.(type) {
	case nil:
		buffer.WriteString("null")
	case bool:
		buffer.WriteString(strconv.FormatBool(typed))
	case string:
		return encodeString(buffer, typed)
	case json.Number:
		number, err := strconv.ParseInt(string(typed), 10, 64)
		if err != nil {
			return errors.New("canonical: numbers must be int64 integers")
		}
		buffer.WriteString(strconv.FormatInt(number, 10))
	case int:
		buffer.WriteString(strconv.FormatInt(int64(typed), 10))
	case int64:
		buffer.WriteString(strconv.FormatInt(typed, 10))
	case []string:
		items := make([]any, len(typed))
		for index, item := range typed {
			items[index] = item
		}
		return encode(buffer, items, depth)
	case []any:
		buffer.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := encode(buffer, item, depth+1); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		// UTF-8 byte order equals code point order, which is Python's sort.
		slices.Sort(keys)
		buffer.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := encode(buffer, key, depth+1); err != nil {
				return err
			}
			buffer.WriteByte(':')
			if err := encode(buffer, typed[key], depth+1); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	default:
		return fmt.Errorf("canonical: unsupported type %T", value)
	}
	return nil
}

func encodeString(buffer *bytes.Buffer, value string) error {
	if !utf8.ValidString(value) {
		return errors.New("canonical: invalid UTF-8")
	}
	buffer.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"':
			buffer.WriteString(`\"`)
		case '\\':
			buffer.WriteString(`\\`)
		case '\n':
			buffer.WriteString(`\n`)
		case '\r':
			buffer.WriteString(`\r`)
		case '\t':
			buffer.WriteString(`\t`)
		case '\b':
			buffer.WriteString(`\b`)
		case '\f':
			buffer.WriteString(`\f`)
		default:
			switch {
			case r >= 0x20 && r <= 0x7e:
				buffer.WriteRune(r)
			case r < 0x10000:
				fmt.Fprintf(buffer, `\u%04x`, r)
			default:
				high, low := utf16.EncodeRune(r)
				fmt.Fprintf(buffer, `\u%04x\u%04x`, high, low)
			}
		}
	}
	buffer.WriteByte('"')
	return nil
}
