# UI primitives

These are **placeholders with shadcn/ui-identical import paths and prop APIs**,
written because this repository has no shadcn/ui installation yet. They are
built on native elements (`<button>`, `<select>`, `<input type="range">`), which
keeps keyboard navigation and screen-reader behaviour correct without Radix.

To adopt the real design system, run the generator — it overwrites these files
and every consuming import keeps working unchanged:

```bash
npx shadcn@latest init
npx shadcn@latest add button card label input select slider radio-group badge skeleton alert separator
```
