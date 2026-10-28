import React from 'react';

const icons = { Laptops:'💻', Monitors:'🖥️', Accessories:'⌨️', Audio:'🎧', Components:'🔧', Networking:'📡', Furniture:'🪑', Storage:'💾' };

function CategoryNav({ categories, active, onSelect }) {
  if (!categories.length) return null;
  return (
    <nav className="flex gap-2 mb-5 overflow-x-auto whitespace-nowrap" aria-label="Categories">
      {categories.map((cat) => (
        <button key={cat} onClick={() => onSelect(cat)}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-all flex items-center gap-2 flex-shrink-0 ${
            active === cat
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-white text-gray-600 border border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
          }`}>
          <span className="text-base">{icons[cat] || '📦'}</span>{cat}
        </button>
      ))}
    </nav>
  );
}

export default CategoryNav;
