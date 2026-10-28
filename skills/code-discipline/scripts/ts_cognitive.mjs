// Cognitive complexity for TypeScript, JavaScript, and Vue <script> blocks.
// Input (stdin):  {"root": "/abs/project", "files": ["src/a.ts", "src/B.vue"]}
// Output (stdout): {"results": [{"path", "status", "message", "functions": [...]}]}
// Rules mirror portable_analysis._CognitiveVisitor, with SonarJS conventions
// where JavaScript differs: `switch` counts once, only labelled break/continue
// count, and each run of identical logical operators counts once.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

const input = JSON.parse(readFileSync(0, 'utf8'));
const ts = loadTypeScript(input.root);
const SCRIPT_BLOCK = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
const LOGICAL_OPERATORS = new Set(['&&', '||', '??']);

function loadTypeScript(root) {
  try {
    return createRequire(join(root, 'package.json'))('typescript');
  } catch {
    return null;
  }
}

function scriptBlocks(path, text) {
  if (!path.endsWith('.vue')) return [{ code: text, lineOffset: 0, tsx: /\.[jt]sx$/.test(path) }];
  return [...text.matchAll(SCRIPT_BLOCK)].map((match) => {
    const start = match.index + match[0].indexOf('>') + 1;
    return {
      code: match[2],
      lineOffset: text.slice(0, start).split('\n').length - 1,
      tsx: /lang=["'](tsx|jsx)["']/.test(match[1]),
    };
  });
}

function isFunctionLike(node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isConstructorDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  );
}

function propertyName(name) {
  if (!name) return null;
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name) || ts.isStringLiteral(name)) return name.text;
  return null;
}

function functionName(node) {
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  const own = propertyName(node.name);
  if (own) return own;
  const parent = node.parent;
  if (parent && (ts.isVariableDeclaration(parent) || ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent))) {
    const assigned = propertyName(parent.name);
    if (assigned) return assigned;
  }
  if (parent && ts.isCallExpression(parent)) {
    const callee = parent.expression;
    const calleeName = ts.isIdentifier(callee) ? callee.text : ts.isPropertyAccessExpression(callee) ? callee.name.text : null;
    if (calleeName) return `${calleeName}(callback)`;
  }
  return '<anonymous>';
}

function scopeName(node) {
  return ts.isClassLike(node) ? propertyName(node.name) ?? '<class>' : null;
}

class CognitiveVisitor {
  constructor(name) {
    this.name = name;
    this.score = 0;
    this.nesting = 0;
    this.maxNesting = 0;
  }

  control() {
    this.score += 1 + this.nesting;
    this.maxNesting = Math.max(this.maxNesting, this.nesting + 1);
  }

  nested(nodes) {
    this.nesting += 1;
    try {
      nodes.filter(Boolean).forEach((node) => this.visit(node));
    } finally {
      this.nesting -= 1;
    }
  }

  visitIf(node) {
    this.control();
    this.visit(node.expression);
    this.nested([node.thenStatement]);
    if (node.elseStatement && ts.isIfStatement(node.elseStatement)) this.visitIf(node.elseStatement);
    else this.nested([node.elseStatement]);
  }

  visitLoop(node) {
    this.control();
    ['initializer', 'expression', 'condition', 'incrementor'].forEach((field) => node[field] && this.visit(node[field]));
    this.nested([node.statement]);
  }

  visitLogical(node) {
    const operator = node.operatorToken.kind;
    const parent = node.parent;
    const continuesRun = parent && ts.isBinaryExpression(parent) && parent.operatorToken.kind === operator;
    if (!continuesRun) this.score += 1;
    ts.forEachChild(node, (child) => this.visit(child));
  }

  visit(node) {
    if (isFunctionLike(node) || ts.isClassLike(node)) return;
    if (ts.isIfStatement(node)) return this.visitIf(node);
    if (ts.isConditionalExpression(node)) {
      this.control();
      this.visit(node.condition);
      return this.nested([node.whenTrue, node.whenFalse]);
    }
    if (ts.isForStatement(node) || ts.isForInStatement(node) || ts.isForOfStatement(node) || ts.isWhileStatement(node) || ts.isDoStatement(node)) {
      return this.visitLoop(node);
    }
    if (ts.isSwitchStatement(node)) {
      this.control();
      this.visit(node.expression);
      return this.nested(node.caseBlock.clauses.flatMap((clause) => [...(clause.expression ? [clause.expression] : []), ...clause.statements]));
    }
    if (ts.isCatchClause(node)) {
      this.control();
      return this.nested([node.block]);
    }
    if ((ts.isBreakStatement(node) || ts.isContinueStatement(node)) && node.label) this.score += 1;
    if (ts.isBinaryExpression(node) && LOGICAL_OPERATORS.has(ts.tokenToString(node.operatorToken.kind))) {
      return this.visitLogical(node);
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === this.name) this.score += 1;
    ts.forEachChild(node, (child) => this.visit(child));
  }
}

function collectFunctions(path, block) {
  const kind = block.tsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(path, block.code, ts.ScriptTarget.Latest, true, kind);
  if (source.parseDiagnostics?.length) {
    const first = source.parseDiagnostics[0];
    const { line } = source.getLineAndCharacterOfPosition(first.start ?? 0);
    throw new Error(`line ${line + 1 + block.lineOffset}`);
  }
  const functions = [];
  const lineOf = (pos) => source.getLineAndCharacterOfPosition(pos).line + 1 + block.lineOffset;
  const walk = (node, scope) => {
    if (isFunctionLike(node)) {
      const name = functionName(node);
      const visitor = new CognitiveVisitor(name);
      if (node.body && ts.isBlock(node.body)) ts.forEachChild(node.body, (child) => visitor.visit(child));
      else if (node.body) visitor.visit(node.body);
      functions.push({
        name: [...scope, name].join('.'),
        line: lineOf(node.getStart(source)),
        end_line: lineOf(node.getEnd()),
        cognitive_complexity: visitor.score,
        max_nesting: visitor.maxNesting,
      });
    }
    const inner = scopeName(node);
    ts.forEachChild(node, (child) => walk(child, inner ? [...scope, inner] : scope));
  };
  walk(source, []);
  return functions;
}

function analyze(path) {
  if (!ts) return { path, language: 'typescript', status: 'unsupported', message: 'cognitive complexity needs the project typescript package', functions: [] };
  try {
    const text = readFileSync(join(input.root, path), 'utf8');
    const functions = scriptBlocks(path, text).flatMap((block) => collectFunctions(path, block));
    return { path, language: 'typescript', status: 'supported', message: '', functions };
  } catch (error) {
    return { path, language: 'typescript', status: 'parse_error', message: String(error.message ?? error), functions: [] };
  }
}

process.stdout.write(JSON.stringify({ results: input.files.map(analyze) }));
