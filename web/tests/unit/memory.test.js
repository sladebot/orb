// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest';
import { escapeHtml, renderBars, renderTags, renderRecent } from '../../static/memory.js';

describe('XSS fix (Issue #29): memory dashboard sanitizes vault metadata', () => {
    it('escapeHtml encodes angle brackets and ampersands', () => {
        const malicious = '<script>alert("xss")</script>';
        const escaped = escapeHtml(malicious);
        // Must NOT contain raw tags
        expect(escaped).not.toContain('<script>');
        expect(escaped).not.toContain('</script>');
        expect(escaped).not.toContain('<');
        expect(escaped).not.toContain('>');
        // Must contain HTML entities
        expect(escaped).toContain('&lt;script&gt;');
    });

    it('escapeHtml encodes ampersands', () => {
        const input = 'a & b & c';
        const escaped = escapeHtml(input);
        expect(escaped).toContain('&amp;');
        expect(escaped).not.toContain(' & ');
    });

    it('escapeHtml leaves safe strings intact', () => {
        expect(escapeHtml('hello')).toBe('hello');
        expect(escapeHtml('')).toBe('');
        expect(escapeHtml('path/to/page.md')).toBe('path/to/page.md');
    });

    it('renderBars escapes label values against XSS', () => {
        const target = document.createElement('div');
        target.id = 'test-bars';
        document.body.appendChild(target);

        const maliciousCounts = {
            '<script>alert(1)</script>': 5,
            'safe_label': 3,
        };
        renderBars(target, maliciousCounts);

        // The escaped string should use HTML entities, not raw tags
        expect(target.innerHTML).not.toContain('<script>');
        expect(target.innerHTML).toContain('&lt;script&gt;');
        expect(target.innerHTML).toContain('safe_label');
    });

    it('renderTags escapes tag values against XSS', () => {
        const target = document.createElement('span');
        target.id = 'test-tags';
        document.body.appendChild(target);

        const maliciousTags = [
            { tag: '<img src=x onerror=alert(1)>', count: 10 },
            { tag: 'normal_tag', count: 2 },
        ];
        renderTags(target, maliciousTags);

        // After HTML escaping, <img becomes &lt;img — the browser does not
        // parse it as an actual <img> element, so onerror never fires.
        expect(target.innerHTML).not.toContain('<img');
        expect(target.innerHTML).toContain('&lt;img');
        expect(target.innerHTML).toContain('normal_tag');
    });

    it('renderRecent escapes title, path, and type against XSS', () => {
        const target = document.createElement('div');
        target.id = 'test-recent';
        document.body.appendChild(target);

        const maliciousPages = [
            {
                title: '<b>bold</b><script>bad()</script>',
                path: '/foo/<script>alert("xss")</script>/bar',
                type: '<iframe src="evil.com">',
            },
            { title: 'normal page.md', path: '/docs/normal.md', type: 'wiki' },
        ];
        renderRecent(target, maliciousPages);

        expect(target.innerHTML).not.toContain('<script>');
        expect(target.innerHTML).not.toContain('<iframe>');
        // All malicious HTML is converted to HTML entities
        expect(target.innerHTML).toContain('&lt;script&gt;');
        expect(target.innerHTML).toContain('&lt;iframe');
        expect(target.innerHTML).toContain('normal page.md');
    });
});
