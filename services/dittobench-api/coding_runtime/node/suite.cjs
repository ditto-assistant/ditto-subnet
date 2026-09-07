'use strict';
// Parse trusted source to a closed instruction set. Never evaluate/transpile/run
// the suite and never load a candidate module into the trusted parent.
const ts = require('./node_modules/typescript');
const NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,79}$/;
const MODULE = /^[A-Za-z][A-Za-z0-9_/-]*\.(?:js|mjs|cjs|ts|mts|cts)$/;
const ASSERTIONS = new Set([
  'equal',
  'strictEqual',
  'notEqual',
  'notStrictEqual',
  'deepEqual',
  'deepStrictEqual',
  'notDeepEqual',
  'notDeepStrictEqual',
  'ok',
]);
const RESERVED = new Set(['undefined', '__proto__', 'prototype', 'constructor']);

class InvalidSuite extends Error {}
function requireSuite(ok) {
  if (!ok) throw new InvalidSuite('unsupported suite');
}
function validName(name) {
  return NAME.test(name) && !RESERVED.has(name);
}
function validModule(name) {
  return (
    typeof name === 'string' &&
    name.length <= 240 &&
    MODULE.test(name) &&
    name.split('/').every((part) => part && part !== '.' && part !== '..')
  );
}

function compileSuite(source, allowedModules) {
  requireSuite(typeof source === 'string' && Buffer.byteLength(source) <= 512 * 1024);
  requireSuite(
    allowedModules.length > 0 &&
      allowedModules.every(validModule) &&
      new Set(allowedModules).size === allowedModules.length,
  );
  const tree = ts.createSourceFile(
    'suite.ts',
    source,
    ts.ScriptTarget.ES2022,
    false,
    ts.ScriptKind.TS,
  );
  requireSuite(tree.parseDiagnostics.length === 0);
  const imports = new Map();
  const tests = [];
  const titles = new Set();
  let nodes = 0;
  let asyncAllowed = false;
  function bind(names, name, value) {
    requireSuite(validName(name) && !names.has(name));
    names.set(name, value);
  }
  function expression(node, names, depth = 0) {
    requireSuite(node && depth <= 32 && ++nodes <= 20000);
    const sub = (value) => expression(value, names, depth + 1);
    if (ts.isParenthesizedExpression(node)) return sub(node.expression);
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
      return { op: 'literal', value: node.text };
    if (ts.isNumericLiteral(node)) {
      const value = Number(node.text);
      requireSuite(Number.isFinite(value));
      return { op: 'literal', value };
    }
    if (ts.isBigIntLiteral(node)) {
      requireSuite(node.text.length <= 4097);
      return { op: 'literal', value: BigInt(node.text.slice(0, -1)) };
    }
    if (node.kind === ts.SyntaxKind.TrueKeyword) return { op: 'literal', value: true };
    if (node.kind === ts.SyntaxKind.FalseKeyword)
      return { op: 'literal', value: false };
    if (node.kind === ts.SyntaxKind.NullKeyword) return { op: 'literal', value: null };
    if (ts.isIdentifier(node)) {
      if (node.text === 'undefined') return { op: 'literal', value: undefined };
      requireSuite(
        names.has(node.text) &&
          ['candidate', 'local'].includes(names.get(node.text).kind),
      );
      return { op: 'name', name: node.text };
    }
    if (ts.isArrayLiteralExpression(node)) {
      requireSuite(node.elements.length <= 4096);
      return { op: 'array', values: node.elements.map(sub) };
    }
    if (ts.isObjectLiteralExpression(node)) {
      const entries = node.properties.map((property) => {
        requireSuite(
          ts.isPropertyAssignment(property) &&
            (ts.isIdentifier(property.name) || ts.isStringLiteral(property.name)),
        );
        return [property.name.text, sub(property.initializer)];
      });
      requireSuite(
        entries.length <= 4096 &&
          new Set(entries.map(([key]) => key)).size === entries.length,
      );
      return { op: 'object', entries };
    }
    if (
      ts.isPrefixUnaryExpression(node) &&
      [ts.SyntaxKind.MinusToken, ts.SyntaxKind.ExclamationToken].includes(node.operator)
    ) {
      return {
        op: node.operator === ts.SyntaxKind.MinusToken ? 'negate' : 'not',
        value: sub(node.operand),
      };
    }
    if (ts.isPropertyAccessExpression(node) && !node.questionDotToken) {
      requireSuite(validName(node.name.text));
      return { op: 'property', base: sub(node.expression), key: node.name.text };
    }
    if (ts.isElementAccessExpression(node) && !node.questionDotToken) {
      const key = sub(node.argumentExpression);
      requireSuite(
        key.op === 'literal' &&
          ((typeof key.value === 'string' && !RESERVED.has(key.value)) ||
            (Number.isSafeInteger(key.value) && key.value >= 0)),
      );
      return { op: 'property', base: sub(node.expression), key: String(key.value) };
    }
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      requireSuite(
        !node.questionDotToken &&
          !node.typeArguments &&
          node.arguments &&
          node.arguments.length <= 128,
      );
      if (
        ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression) &&
        ts.isIdentifier(node.expression.expression) &&
        names.get(node.expression.expression.text)?.kind === 'buffer'
      ) {
        requireSuite(
          node.expression.name.text === 'from' &&
            !node.expression.questionDotToken &&
            node.arguments.length >= 1 &&
            node.arguments.length <= 2,
        );
        const input = sub(node.arguments[0]);
        if (input.op === 'array') {
          requireSuite(
            node.arguments.length === 1 &&
              input.values.every(
                (item) =>
                  item.op === 'literal' &&
                  Number.isInteger(item.value) &&
                  item.value >= 0 &&
                  item.value <= 255,
              ),
          );
          return {
            op: 'literal',
            value: Buffer.from(input.values.map((item) => item.value)),
          };
        }
        requireSuite(input.op === 'literal' && typeof input.value === 'string');
        const encoding =
          node.arguments.length === 1
            ? { op: 'literal', value: 'utf8' }
            : sub(node.arguments[1]);
        requireSuite(
          encoding.op === 'literal' &&
            ['utf8', 'hex', 'base64'].includes(encoding.value),
        );
        const bytes = Buffer.from(input.value, encoding.value);
        requireSuite(
          encoding.value === 'utf8' || bytes.toString(encoding.value) === input.value,
        );
        return { op: 'literal', value: bytes };
      }
      return {
        op: ts.isNewExpression(node) ? 'construct' : 'call',
        target: sub(node.expression),
        args: node.arguments.map(sub),
      };
    }
    if (ts.isAwaitExpression(node)) {
      requireSuite(asyncAllowed);
      return { op: 'await', value: sub(node.expression) };
    }
    if (ts.isBinaryExpression(node)) {
      const operator = ts.tokenToString(node.operatorToken.kind);
      requireSuite(
        ['===', '!==', '+', '-', '*', '<', '<=', '>', '>='].includes(operator),
      );
      return { op: 'binary', operator, left: sub(node.left), right: sub(node.right) };
    }
    throw new InvalidSuite('unsupported expression');
  }
  function assertion(call) {
    if (!ts.isCallExpression(call) || call.questionDotToken || call.typeArguments)
      return null;
    if (ts.isIdentifier(call.expression)) {
      const imported = imports.get(call.expression.text);
      if (imported?.kind === 'assertion') return imported.method;
      if (imported?.kind === 'assert') return 'ok';
    }
    if (
      ts.isPropertyAccessExpression(call.expression) &&
      !call.expression.questionDotToken &&
      ts.isIdentifier(call.expression.expression) &&
      imports.get(call.expression.expression.text)?.kind === 'assert'
    ) {
      requireSuite(ASSERTIONS.has(call.expression.name.text));
      return call.expression.name.text;
    }
    return null;
  }
  for (const statement of tree.statements) {
    if (ts.isImportDeclaration(statement)) {
      requireSuite(
        tests.length === 0 &&
          !statement.modifiers &&
          !statement.attributes &&
          ts.isStringLiteral(statement.moduleSpecifier) &&
          statement.importClause &&
          !statement.importClause.isTypeOnly,
      );
      const specifier = statement.moduleSpecifier.text;
      let kind, module;
      if (specifier === 'node:assert/strict') kind = 'assert';
      else if (specifier === 'node:test') kind = 'test';
      else if (specifier === 'node:buffer') kind = 'buffer';
      else {
        module = specifier.startsWith('./') ? specifier.slice(2) : '';
        requireSuite(allowedModules.includes(module));
        kind = 'candidate';
      }
      const clause = statement.importClause;
      if (clause.name) {
        requireSuite(kind !== 'buffer');
        bind(imports, clause.name.text, { kind, module, path: ['default'] });
      }
      if (clause.namedBindings) {
        if (ts.isNamespaceImport(clause.namedBindings)) {
          requireSuite(!['test', 'buffer'].includes(kind));
          bind(imports, clause.namedBindings.name.text, { kind, module, path: [] });
        } else {
          for (const item of clause.namedBindings.elements) {
            const exported = (item.propertyName ?? item.name).text;
            requireSuite(!item.isTypeOnly && validName(exported));
            if (kind === 'test') requireSuite(exported === 'test');
            if (kind === 'buffer') requireSuite(exported === 'Buffer');
            if (kind === 'assert') requireSuite(ASSERTIONS.has(exported));
            bind(
              imports,
              item.name.text,
              kind === 'assert'
                ? { kind: 'assertion', method: exported }
                : { kind, module, path: [exported] },
            );
          }
        }
      }
      continue;
    }
    requireSuite(
      ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression),
    );
    const call = statement.expression;
    requireSuite(
      ts.isIdentifier(call.expression) &&
        imports.get(call.expression.text)?.kind === 'test' &&
        !call.questionDotToken &&
        !call.typeArguments &&
        call.arguments.length === 2 &&
        ts.isStringLiteral(call.arguments[0]),
    );
    const title = call.arguments[0].text;
    requireSuite(
      title.length > 0 &&
        title.length <= 200 &&
        !titles.has(title) &&
        tests.length < 1024,
    );
    titles.add(title);
    const fn = call.arguments[1];
    requireSuite(
      (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) &&
        !fn.name &&
        !fn.asteriskToken &&
        fn.parameters.length === 0 &&
        !fn.typeParameters &&
        ts.isBlock(fn.body) &&
        (!fn.modifiers ||
          fn.modifiers.every((m) => m.kind === ts.SyntaxKind.AsyncKeyword)),
    );
    const names = new Map(imports);
    asyncAllowed = Boolean(
      fn.modifiers?.some((m) => m.kind === ts.SyntaxKind.AsyncKeyword),
    );
    const instructions = [];
    let assertions = 0;
    for (const item of fn.body.statements) {
      if (ts.isVariableStatement(item)) {
        requireSuite(
          !item.modifiers &&
            (item.declarationList.flags & (ts.NodeFlags.Const | ts.NodeFlags.Let)) !==
              0,
        );
        for (const decl of item.declarationList.declarations) {
          requireSuite(
            ts.isIdentifier(decl.name) && decl.initializer && !decl.exclamationToken,
          );
          const value = expression(decl.initializer, names);
          bind(names, decl.name.text, { kind: 'local' });
          instructions.push({ op: 'bind', name: decl.name.text, value });
        }
      } else {
        requireSuite(ts.isExpressionStatement(item));
        const method = assertion(item.expression);
        if (method) {
          requireSuite(item.expression.arguments.length === (method === 'ok' ? 1 : 2));
          instructions.push({
            op: 'assert',
            method,
            args: item.expression.arguments.map((arg) => expression(arg, names)),
          });
          assertions++;
        } else {
          const value = expression(item.expression, names);
          requireSuite(['call', 'await'].includes(value.op));
          instructions.push({ op: 'discard', value });
        }
      }
    }
    requireSuite(assertions > 0);
    tests.push(instructions);
  }
  requireSuite(
    tests.length > 0 &&
      [...imports.values()].some((value) => value.kind === 'candidate'),
  );
  return { imports, tests };
}

module.exports = { compileSuite, InvalidSuite, validModule };
