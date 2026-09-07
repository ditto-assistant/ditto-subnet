package codinggooracle

import "errors"

var errPrivateSerialization = errors.New("private Go oracle material is not a wire object")

// These are process-local objects, not API/logging DTOs. Explicitly reject the
// common JSON and formatting paths so accidental debug output cannot dump tests.
func (Source) String() string               { return "<private Go source>" }
func (Source) GoString() string             { return "<private Go source>" }
func (Source) MarshalJSON() ([]byte, error) { return nil, errPrivateSerialization }
func (Config) String() string               { return "<private Go oracle configuration>" }
func (Config) GoString() string             { return "<private Go oracle configuration>" }
func (Config) MarshalJSON() ([]byte, error) { return nil, errPrivateSerialization }
func (*Suite) String() string               { return "<private Go oracle suite>" }
func (*Suite) GoString() string             { return "<private Go oracle suite>" }
func (*Suite) MarshalJSON() ([]byte, error) { return nil, errPrivateSerialization }
