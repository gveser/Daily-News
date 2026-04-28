## Layout backup (v1)

This file is a quick backup of the previous **vertical** card layout so it can be restored later.

### Key CSS snippets (v1)

```css
.rowCards {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 14px;
}

@media (max-width: 1100px) {
  .rowCards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 640px) {
  .rowCards { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .summary { display: none !important; }
  .card { min-height: 230px; grid-template-rows: 110px auto; }
}

.card {
  display: grid;
  grid-template-rows: 148px auto;
  min-height: 320px;
}

.card.noMedia {
  grid-template-rows: auto;
  min-height: 220px;
}
```

### Key HTML structure (v1)

```html
<a class="card" href="...">
  <div class="media"><img class="thumb" src="..." /></div>
  <div class="meta">
    <div class="title">...</div>
    <div class="summary">...</div>
  </div>
</a>
```

