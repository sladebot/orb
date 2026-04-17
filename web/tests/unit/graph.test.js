import { describe, it, expect } from 'vitest';
import { roundRect, glyphKeyFor, alphaHex, tintColor, NODE_RADIUS } from '../../static/graph.js';

// Minimal Canvas2D stub — records path commands in call-order so we can assert
// shape geometry without a real browser canvas.
function makeCtx() {
    const calls = [];
    const rec = (name) => (...args) => calls.push([name, ...args]);
    return {
        calls,
        beginPath: rec('beginPath'),
        moveTo:    rec('moveTo'),
        lineTo:    rec('lineTo'),
        arcTo:     rec('arcTo'),
        closePath: rec('closePath'),
    };
}

describe('roundRect (chip silhouette)', () => {
    it('opens a path at (x + r, y) and traces 4 corners', () => {
        const ctx = makeCtx();
        roundRect(ctx, 10, 20, 100, 40, NODE_RADIUS);

        const cmds = ctx.calls.map((c) => c[0]);
        expect(cmds[0]).toBe('beginPath');
        expect(cmds.filter((c) => c === 'arcTo')).toHaveLength(4);
        expect(cmds.at(-1)).toBe('closePath');

        const firstMove = ctx.calls.find((c) => c[0] === 'moveTo');
        expect(firstMove).toEqual(['moveTo', 10 + NODE_RADIUS, 20]);
    });
});

describe('glyphKeyFor', () => {
    it('maps known agent ids to role glyphs', () => {
        expect(glyphKeyFor('coordinator', '')).toBe('coordinator');
        expect(glyphKeyFor('coder', 'Writes code')).toBe('coder');
        expect(glyphKeyFor('reviewer_a', 'Quality')).toBe('reviewer');
        expect(glyphKeyFor('reviewer_b', 'Security')).toBe('reviewer');
        expect(glyphKeyFor('tester', 'Runs tests')).toBe('tester');
        expect(glyphKeyFor('researcher', 'Investigates')).toBe('researcher');
    });

    it('infers from role text when id is unknown', () => {
        expect(glyphKeyFor('agent_7', 'Implementer')).toBe('coder');
        expect(glyphKeyFor('agent_x', 'Validator')).toBe('tester');
        expect(glyphKeyFor('agent_z', 'Investigator')).toBe('researcher');
    });

    it('falls back to "generic" when nothing matches', () => {
        expect(glyphKeyFor('mystery', 'Unknown')).toBe('generic');
        expect(glyphKeyFor('', '')).toBe('generic');
    });
});

describe('alphaHex', () => {
    it('produces rgba with the requested alpha', () => {
        expect(alphaHex('#94bfff', 0.5)).toBe('rgba(148, 191, 255, 0.5)');
        expect(alphaHex('86d8ab', 0)).toBe('rgba(134, 216, 171, 0)');
    });

    it('falls back to the primary tint when the input is invalid', () => {
        expect(alphaHex('not-hex', 0.3)).toBe('rgba(148, 191, 255, 0.3)');
        expect(alphaHex(null, 0.2)).toBe('rgba(148, 191, 255, 0.2)');
    });
});

describe('tintColor', () => {
    it('returns the pure hex when mix = 1', () => {
        expect(tintColor('#94bfff', 1, '#000000')).toBe('rgb(148, 191, 255)');
    });

    it('returns the fallback when mix = 0', () => {
        expect(tintColor('#94bfff', 0, '#101010')).toBe('rgb(16, 16, 16)');
    });

    it('blends proportionally between the two colors', () => {
        const blended = tintColor('#ffffff', 0.5, '#000000');
        expect(blended).toBe('rgb(128, 128, 128)');
    });

    it('returns fallback literal on invalid input', () => {
        expect(tintColor('garbage', 0.5, '#123456')).toBe('#123456');
    });
});
