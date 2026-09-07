'use strict';
// Explicit envelopes keep user object keys separate from transport tags.
// Child-side values are proposals, never trusted language-runtime attestations.
const MAX_DEPTH = 32;
const MAX_ITEMS = 4096;
class PromiseValue {
  constructor(state, value) {
    this.state = state;
    this.value = value;
  }
}
class ErrorValue {
  constructor(message) {
    this.message = message;
  }
}
class CallbackValue {
  constructor(reference) {
    this.reference = reference;
  }
}

function materialize(value, callback) {
  if (value instanceof CallbackValue) {
    if (!callback) throw new Error('callback transport unavailable');
    return (...args) => callback(value.reference, args);
  }
  if (value instanceof ErrorValue) return new Error(value.message);
  if (value instanceof PromiseValue) {
    const promise =
      value.state === 'fulfilled'
        ? Promise.resolve(materialize(value.value, callback))
        : Promise.reject(materialize(value.value, callback));
    promise.catch(() => {});
    return promise;
  }
  if (Array.isArray(value)) return value.map((item) => materialize(item, callback));
  if (value && Object.getPrototypeOf(value) === Object.prototype)
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, materialize(item, callback)]),
    );
  return value;
}

function pack(value, depth = 0, state = { count: 0 }) {
  if (depth > MAX_DEPTH || ++state.count > MAX_ITEMS) throw new Error('value bound');
  const nested = (item) => pack(item, depth + 1, state);
  if (
    value instanceof CallbackValue &&
    Number.isSafeInteger(value.reference) &&
    value.reference > 0 &&
    value.reference <= 64
  )
    return ['callback', value.reference];
  if (value instanceof PromiseValue && ['fulfilled', 'rejected'].includes(value.state))
    return ['promise', [value.state, nested(value.value)]];
  if (
    value instanceof ErrorValue &&
    typeof value.message === 'string' &&
    value.message.length <= 4096
  )
    return ['error', value.message];
  if (value === undefined) return ['undefined'];
  if (value === null) return ['null'];
  if (typeof value === 'boolean') return ['boolean', value];
  if (typeof value === 'string') return ['string', value];
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Object.is(value, -0) ? ['negative-zero'] : ['number', value];
  }
  if (typeof value === 'bigint') {
    const encoded = value.toString();
    if (encoded.length > 4096) throw new Error('bigint bound');
    return ['bigint', encoded];
  }
  if (Buffer.isBuffer(value)) return ['buffer', value.toString('base64')];
  if (
    value instanceof Uint8Array &&
    Object.getPrototypeOf(value) === Uint8Array.prototype
  )
    return ['uint8array', Buffer.from(value).toString('base64')];
  if (Array.isArray(value)) {
    if (
      Object.keys(value).length !== value.length ||
      Object.getOwnPropertyNames(value).length !== value.length + 1 ||
      Object.getOwnPropertySymbols(value).length !== 0 ||
      !Array.from({ length: value.length }, (_, i) => Object.hasOwn(value, i)).every(
        Boolean,
      )
    ) {
      throw new Error('non-plain array');
    }
    return ['array', value.map(nested)];
  }
  if (
    typeof value === 'object' &&
    Object.getPrototypeOf(value) === Object.prototype &&
    Object.getOwnPropertySymbols(value).length === 0
  ) {
    if (
      Object.values(Object.getOwnPropertyDescriptors(value)).some(
        (d) => !d.enumerable || !Object.hasOwn(d, 'value'),
      )
    ) {
      throw new Error('non-plain object');
    }
    return ['object', Object.keys(value).map((key) => [key, nested(value[key])])];
  }
  throw new Error('not a transport value');
}

function unpack(wire, depth = 0, state = { count: 0 }) {
  if (depth > MAX_DEPTH || ++state.count > MAX_ITEMS || !Array.isArray(wire)) {
    throw new Error('invalid wire value');
  }
  const [kind, value] = wire;
  if (wire.length === 1) {
    if (kind === 'undefined') return undefined;
    if (kind === 'null') return null;
    if (kind === 'negative-zero') return -0;
  }
  if (wire.length !== 2) throw new Error('invalid wire arity');
  const nested = (item) => unpack(item, depth + 1, state);
  if (kind === 'callback' && Number.isSafeInteger(value) && value > 0 && value <= 64)
    return new CallbackValue(value);
  if (
    kind === 'promise' &&
    Array.isArray(value) &&
    value.length === 2 &&
    ['fulfilled', 'rejected'].includes(value[0])
  )
    return new PromiseValue(value[0], nested(value[1]));
  if (kind === 'error' && typeof value === 'string' && value.length <= 4096)
    return new ErrorValue(value);
  if (kind === 'boolean' && typeof value === 'boolean') return value;
  if (kind === 'string' && typeof value === 'string') return value;
  if (kind === 'number' && typeof value === 'number' && Number.isFinite(value))
    return value;
  if (
    kind === 'bigint' &&
    typeof value === 'string' &&
    /^(0|-?[1-9][0-9]*)$/.test(value) &&
    value.length <= 4096
  )
    return BigInt(value);
  if (['buffer', 'uint8array'].includes(kind) && typeof value === 'string') {
    const decoded = Buffer.from(value, 'base64');
    if (decoded.toString('base64') !== value) throw new Error('invalid base64');
    return kind === 'buffer' ? decoded : new Uint8Array(decoded);
  }
  if (kind === 'array' && Array.isArray(value)) return value.map(nested);
  if (kind === 'object' && Array.isArray(value)) {
    const entries = value.map((entry) => {
      if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== 'string')
        throw new Error('invalid object');
      return [entry[0], nested(entry[1])];
    });
    if (new Set(entries.map(([key]) => key)).size !== entries.length)
      throw new Error('duplicate key');
    return Object.fromEntries(entries);
  }
  throw new Error('unsupported wire value');
}

module.exports = {
  pack,
  unpack,
  PromiseValue,
  ErrorValue,
  CallbackValue,
  materialize,
};
