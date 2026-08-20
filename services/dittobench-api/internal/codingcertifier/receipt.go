package codingcertifier

import (
	"encoding/json"
	"errors"
	"reflect"
)

const maxReceiptBytes = 128 << 10

// ParseReceipt accepts forward-compatible unknown fields while requiring every
// current known field, rejecting duplicate JSON names, and verifying the
// canonical content digest.
func ParseReceipt(body []byte) (Receipt, error) {
	var receipt Receipt
	if len(body) == 0 || len(body) > maxReceiptBytes {
		return receipt, errors.New("coding certification receipt is outside its size bound")
	}
	if err := validateJSONEnvelope(body); err != nil {
		return receipt, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return receipt, errors.New("coding certification receipt JSON is invalid")
	}
	typeOfReceipt := reflect.TypeOf(Receipt{})
	for index := range typeOfReceipt.NumField() {
		name := typeOfReceipt.Field(index).Tag.Get("json")
		if _, exists := fields[name]; !exists {
			return Receipt{}, errors.New("coding certification receipt lacks a known field")
		}
	}
	if err := json.Unmarshal(body, &receipt); err != nil {
		return Receipt{}, errors.New("coding certification receipt fields are invalid")
	}
	if err := receipt.Validate(); err != nil {
		return Receipt{}, err
	}
	return receipt, nil
}
