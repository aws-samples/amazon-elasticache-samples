"""Tests for the calculator module."""

import pytest
from agentic_shopping_demo.calculator import add, multiply


class TestAdd:
    """Tests for the add function."""

    def test_add_positive_numbers(self):
        """Test adding two positive numbers."""
        assert add(2, 3) == 5

    def test_add_negative_numbers(self):
        """Test adding two negative numbers."""
        assert add(-2, -3) == -5

    def test_add_mixed_numbers(self):
        """Test adding positive and negative numbers."""
        assert add(5, -3) == 2

    def test_add_floats(self):
        """Test adding floating point numbers."""
        assert add(2.5, 3.7) == pytest.approx(6.2)


class TestMultiply:
    """Tests for the multiply function."""

    def test_multiply_positive_numbers(self):
        """Test multiplying two positive numbers."""
        assert multiply(3, 4) == 12

    def test_multiply_by_zero(self):
        """Test multiplying by zero."""
        assert multiply(5, 0) == 0

    def test_multiply_negative_numbers(self):
        """Test multiplying two negative numbers."""
        assert multiply(-2, -3) == 6

    def test_multiply_floats(self):
        """Test multiplying floating point numbers."""
        assert multiply(2.5, 4.0) == pytest.approx(10.0)
