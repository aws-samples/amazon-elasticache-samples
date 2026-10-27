import React from 'react';

const categoryIcons = {
  Laptops: '💻', Monitors: '🖥️', Accessories: '⌨️', Audio: '🎧',
  Components: '🔧', Networking: '📡', Furniture: '🪑', Storage: '💾',
};

function RecommendationRow({ products }) {
  return (
    <div className="flex gap-3 overflow-x-auto pb-1">
      {products.map((product) => (
        <div key={product.id} className="flex-shrink-0 flex items-center gap-2 bg-white border border-gray-100 rounded-lg px-3 py-2 hover:shadow-sm transition-shadow min-w-[200px] max-w-[240px]">
          <span className="text-xl">{categoryIcons[product.category] || '📦'}</span>
          <div className="min-w-0">
            <p className="text-xs font-medium text-gray-900 truncate">{product.name}</p>
            <p className="text-xs text-gray-500">${product.price.toFixed(2)}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

export default RecommendationRow;
