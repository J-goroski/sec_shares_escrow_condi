"""
=============================================================================
PYTHON FUNDAMENTALS REFERENCE GUIDE
=============================================================================
Quick reference for core Python concepts, data structures, methods, 
algorithms, and syntax patterns. Designed for review and lookup.

Topics:
  1. Basic Types & Operations
  2. Data Structures (Lists, Dicts, Sets, Tuples)
  3. String Operations & Methods
  4. Control Flow & Iteration
  5. Functions & Decorators
  6. Object-Oriented Programming
  7. File I/O & Context Managers
  8. Comprehensions & Generators
  9. Error Handling
  10. Common Algorithms & Patterns
  11. Functional Programming (map, filter, reduce)
  12. Regular Expressions
  13. Datetime Handling
  14. Useful Built-ins & Tips

=============================================================================
1. BASIC TYPES & OPERATIONS
=============================================================================
"""

# ---- NUMERIC TYPES ----
a = 10              # int
b = 3.14            # float
c = 2 + 3j          # complex (real + imaginary)

# Arithmetic operators
result = a + b      # addition
result = a - b      # subtraction
result = a * b      # multiplication
result = a / b      # division (always returns float)
result = a // b     # floor division (integer division)
result = a % b      # modulo (remainder)
result = a ** 2     # exponentiation (power)

# Comparison operators return booleans
a == b              # equal to
a != b              # not equal to
a < b               # less than
a <= b              # less than or equal to
a > b               # greater than
a >= b              # greater than or equal to

# Logical operators
True and False      # both must be True
True or False       # at least one must be True
not True            # negation

# Type conversion
int(3.14)           # 3
float(10)           # 10.0
str(42)             # "42"
bool(0)             # False (0, empty string, None, empty list = False)
bool(1)             # True (any non-zero number = True)

# ---- VARIABLE ASSIGNMENT ----
x = y = z = 0       # multiple assignment (all = 0)
a, b, c = 1, 2, 3  # unpacking (a=1, b=2, c=3)
x, *rest = [1, 2, 3, 4, 5]  # x=1, rest=[2,3,4,5] (unpacking with *)


"""
=============================================================================
2. DATA STRUCTURES
=============================================================================
"""

# ---- LISTS (mutable, ordered, allow duplicates) ----
my_list = [1, 2, 3, 4, 5]
my_list = []                    # empty list

# List indexing (0-based)
first = my_list[0]              # access by index
last = my_list[-1]              # negative index: -1 is last element
subset = my_list[1:4]           # slicing: indices 1, 2, 3 (4 excluded)
subset = my_list[::2]           # every 2nd element
subset = my_list[::-1]          # reverse

# List methods (mutate the list in place)
my_list.append(6)               # add to end: [1,2,3,4,5,6]
my_list.extend([7, 8])          # add multiple: [1,2,3,4,5,6,7,8]
my_list.insert(0, 0)            # insert at index: [0,1,2,3,4,5,6,7,8]
my_list.remove(3)               # remove first occurrence of value
my_list.pop()                   # remove & return last element
my_list.pop(0)                  # remove & return at index 0
my_list.clear()                 # remove all elements
my_list.sort()                  # sort in-place (ascending)
my_list.sort(reverse=True)      # sort descending
my_list.reverse()               # reverse in-place
index = my_list.index(5)        # find index of value (raises ValueError if not found)
count = my_list.count(5)        # count occurrences of value
my_list.copy()                  # shallow copy

# List methods (don't mutate)
len(my_list)                    # length
sum(my_list)                    # sum of elements
max(my_list)                    # maximum
min(my_list)                    # minimum
all(my_list)                    # all elements truthy?
any(my_list)                    # any element truthy?

# ---- DICTIONARIES (mutable, unordered key-value pairs) ----
my_dict = {'name': 'Jared', 'age': 25, 'city': 'Fort Mill'}
my_dict = {}                    # empty dict
my_dict = dict()                # alternative empty dict

# Access & modify
value = my_dict['name']         # access by key (raises KeyError if not found)
value = my_dict.get('name')     # safe access (returns None if not found)
value = my_dict.get('missing', 'default')  # returns 'default' if missing

my_dict['age'] = 26             # modify existing key
my_dict['country'] = 'USA'      # add new key-value pair
del my_dict['city']             # delete key-value pair

# Dictionary methods
my_dict.keys()                  # view of all keys
my_dict.values()                # view of all values
my_dict.items()                 # view of key-value pairs: dict_items([...])
my_dict.pop('age')              # remove & return value
my_dict.popitem()               # remove & return last key-value pair
my_dict.clear()                 # remove all items
my_dict.update({'age': 27})     # merge another dict (overwrites duplicates)
my_dict.copy()                  # shallow copy

# Dictionary iteration
for key in my_dict:             # iterate over keys
    pass
for value in my_dict.values():  # iterate over values
    pass
for key, value in my_dict.items():  # iterate over both
    pass

# ---- SETS (mutable, unordered, no duplicates) ----
my_set = {1, 2, 3, 4, 5}
my_set = set()                  # empty set (note: {} is empty dict, not set)

# Set operations
my_set.add(6)                   # add element
my_set.remove(3)                # remove element (raises KeyError if not found)
my_set.discard(3)               # remove element (no error if not found)
my_set.pop()                    # remove & return arbitrary element
my_set.clear()                  # remove all

# Set math
set_a = {1, 2, 3}
set_b = {3, 4, 5}
union = set_a | set_b           # {1, 2, 3, 4, 5}
intersection = set_a & set_b    # {3}
difference = set_a - set_b      # {1, 2}
symmetric_diff = set_a ^ set_b  # {1, 2, 4, 5} (in one or the other, not both)

# Membership testing
3 in my_set                     # O(1) average case (faster than lists)
my_set.issubset(set_b)          # all elements in my_set in set_b?
my_set.issuperset(set_b)        # all elements of set_b in my_set?
my_set.isdisjoint(set_b)        # no common elements?

# ---- TUPLES (immutable, ordered, allow duplicates) ----
my_tuple = (1, 2, 3, 4, 5)
my_tuple = ()                   # empty tuple
my_tuple = (1,)                 # single element (note the comma!)

# Tuple indexing (same as lists)
first = my_tuple[0]
subset = my_tuple[1:4]
last = my_tuple[-1]

# Tuple methods (limited—only count and index)
count = my_tuple.count(3)
index = my_tuple.index(3)

# Tuples as dict keys (lists cannot be)
my_dict = {(1, 2): 'coordinate', (3, 4): 'another'}

# Named tuples (more readable)
from collections import namedtuple
Point = namedtuple('Point', ['x', 'y'])
p = Point(1, 2)
print(p.x)                      # 1


"""
=============================================================================
3. STRING OPERATIONS & METHODS
=============================================================================
"""

s = "Hello, World!"

# String indexing & slicing (same as lists)
first_char = s[0]              # 'H'
substring = s[0:5]             # 'Hello'
reversed_s = s[::-1]           # '!dlroW ,olleH'

# String methods (strings are immutable, so methods return new strings)
s.lower()                       # 'hello, world!'
s.upper()                       # 'HELLO, WORLD!'
s.capitalize()                  # 'Hello, world!'
s.title()                       # 'Hello, World!'
s.strip()                       # remove leading/trailing whitespace
s.lstrip()                      # remove leading whitespace
s.rstrip()                      # remove trailing whitespace
s.replace('World', 'Python')    # 'Hello, Python!'
s.split(',')                    # ['Hello', ' World!'] (split by delimiter)
s.split()                       # ['Hello,', 'World!'] (split by whitespace)
','.join(['Hello', 'World'])    # 'Hello,World' (join list with delimiter)

s.startswith('Hello')           # True
s.endswith('!')                 # True
s.find('World')                 # 7 (index of substring, -1 if not found)
s.index('World')                # 7 (like find but raises ValueError if not found)
s.count('l')                    # 3 (occurrences of substring)
s.isdigit()                     # False (all characters are digits?)
s.isalpha()                     # False (all characters are letters?)
s.isalnum()                     # False (all characters alphanumeric?)
s.islower()                     # False (all letters lowercase?)
s.isupper()                     # False (all letters uppercase?)

# String formatting
name = "Jared"
age = 25

# f-strings (most modern, Python 3.6+)
formatted = f"My name is {name} and I'm {age}"
formatted = f"Calculation: {2 + 2}"
formatted = f"Float: {3.14159:.2f}"  # format to 2 decimal places

# .format() method
formatted = "My name is {} and I'm {}".format(name, age)
formatted = "My name is {0} and I'm {1}".format(name, age)
formatted = "My name is {n} and I'm {a}".format(n=name, a=age)

# Old-style formatting (less common now)
formatted = "My name is %s and I'm %d" % (name, age)

# String repetition & concatenation
repeated = "Ha" * 3             # "HaHaHa"
concatenated = "Hello" + " " + "World"


"""
=============================================================================
4. CONTROL FLOW & ITERATION
=============================================================================
"""

# ---- IF / ELIF / ELSE ----
x = 10

if x > 15:
    print("x is greater than 15")
elif x > 5:
    print("x is between 5 and 15")
else:
    print("x is 5 or less")

# Ternary operator (conditional expression)
result = "even" if x % 2 == 0 else "odd"  # result = "even"

# ---- FOR LOOPS ----
for i in range(5):              # i = 0, 1, 2, 3, 4
    print(i)

for i in range(2, 10, 2):       # i = 2, 4, 6, 8 (start, stop, step)
    print(i)

my_list = ['a', 'b', 'c']
for item in my_list:            # iterate over items
    print(item)

for index, item in enumerate(my_list):  # iterate with index
    print(f"{index}: {item}")    # 0: a, 1: b, 2: c

# Zip multiple iterables together
list1 = [1, 2, 3]
list2 = ['a', 'b', 'c']
for num, letter in zip(list1, list2):   # (1, 'a'), (2, 'b'), (3, 'c')
    print(f"{num}: {letter}")

# ---- WHILE LOOPS ----
count = 0
while count < 5:
    print(count)
    count += 1

# ---- BREAK & CONTINUE ----
for i in range(10):
    if i == 3:
        continue                # skip to next iteration
    if i == 7:
        break                   # exit loop
    print(i)

# ---- ELSE CLAUSE (optional, executes if loop completes without break) ----
for i in range(5):
    if i == 10:
        break
else:
    print("Loop completed without break")  # This prints

for i in range(5):
    if i == 3:
        break
else:
    print("Loop completed without break")  # This doesn't print


"""
=============================================================================
5. FUNCTIONS & DECORATORS
=============================================================================
"""

# ---- BASIC FUNCTION ----
def greet(name, greeting="Hello"):
    """
    Greet someone with a custom greeting.
    
    Args:
        name (str): Person's name
        greeting (str): Custom greeting (default: "Hello")
    
    Returns:
        str: Formatted greeting
    """
    return f"{greeting}, {name}!"

# Call the function
result = greet("Jared")                 # "Hello, Jared!"
result = greet("Jared", greeting="Hi") # "Hi, Jared!"
result = greet(name="Jared")            # "Hello, Jared!" (keyword argument)

# ---- VARIABLE NUMBER OF ARGUMENTS ----
def sum_all(*args):
    """Sum any number of arguments."""
    return sum(args)

sum_all(1, 2, 3, 4, 5)         # 15

def print_kwargs(**kwargs):
    """Print key-value arguments."""
    for key, value in kwargs.items():
        print(f"{key}: {value}")

print_kwargs(name="Jared", age=25)  # name: Jared, age: 25

def flexible_func(a, b, *args, **kwargs):
    """Combine positional, variable positional, and keyword arguments."""
    print(f"a={a}, b={b}")
    print(f"args={args}")
    print(f"kwargs={kwargs}")

flexible_func(1, 2, 3, 4, x=10, y=20)
# a=1, b=2
# args=(3, 4)
# kwargs={'x': 10, 'y': 20}

# ---- LAMBDA (anonymous functions) ----
square = lambda x: x ** 2
square(5)                       # 25

# Common use: pass to higher-order functions
numbers = [1, 2, 3, 4, 5]
squared = map(lambda x: x ** 2, numbers)  # [1, 4, 9, 16, 25]

# ---- DECORATORS (functions that modify other functions) ----
def my_decorator(func):
    """A simple decorator that prints before and after the function."""
    def wrapper(*args, **kwargs):
        print("Before function call")
        result = func(*args, **kwargs)
        print("After function call")
        return result
    return wrapper

@my_decorator
def say_hello(name):
    print(f"Hello, {name}!")

say_hello("Jared")
# Before function call
# Hello, Jared!
# After function call

# Decorators with arguments (more advanced)
def repeat(times):
    """Decorator that repeats function execution."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            results = []
            for _ in range(times):
                results.append(func(*args, **kwargs))
            return results
        return wrapper
    return decorator

@repeat(3)
def greet_repeat(name):
    return f"Hello, {name}!"

greet_repeat("Jared")           # ['Hello, Jared!', 'Hello, Jared!', 'Hello, Jared!']


"""
=============================================================================
6. OBJECT-ORIENTED PROGRAMMING (OOP)
=============================================================================
"""

# ---- BASIC CLASS ----
class Person:
    """A simple Person class."""
    
    # Class variable (shared by all instances)
    species = "Homo sapiens"
    
    def __init__(self, name, age):
        """Constructor (initializer)."""
        self.name = name        # instance variable
        self.age = age
    
    def __str__(self):
        """String representation (for print)."""
        return f"Person({self.name}, {self.age})"
    
    def __repr__(self):
        """Developer representation."""
        return f"Person('{self.name}', {self.age})"
    
    def greet(self):
        """Instance method."""
        return f"Hello, I'm {self.name}"
    
    @classmethod
    def from_birth_year(cls, name, birth_year):
        """Class method (receives class, not instance)."""
        age = 2024 - birth_year
        return cls(name, age)
    
    @staticmethod
    def is_adult(age):
        """Static method (doesn't need instance or class)."""
        return age >= 18
    
    @property
    def info(self):
        """Property (access like attribute, not method)."""
        return f"{self.name} is {self.age} years old"

# Create instances
person1 = Person("Jared", 25)
print(person1.greet())          # Hello, I'm Jared
print(person1.info)             # Jared is 25 years old
print(person1.species)          # Homo sapiens

# Create using class method
person2 = Person.from_birth_year("Alice", 1999)
print(person2.age)              # 25

# Use static method
Person.is_adult(25)             # True

# ---- INHERITANCE ----
class Student(Person):
    """A Student inherits from Person."""
    
    def __init__(self, name, age, student_id):
        super().__init__(name, age)  # call parent constructor
        self.student_id = student_id
    
    def greet(self):
        """Override parent method."""
        return f"{super().greet()} I'm a student."

student = Student("Bob", 20, "S12345")
print(student.greet())          # Hello, I'm Bob I'm a student.

# ---- SPECIAL METHODS (dunder methods) ----
class Vector:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    
    def __str__(self):
        return f"Vector({self.x}, {self.y})"
    
    def __eq__(self, other):
        """Equality comparison."""
        return self.x == other.x and self.y == other.y
    
    def __add__(self, other):
        """Addition operator."""
        return Vector(self.x + other.x, self.y + other.y)
    
    def __sub__(self, other):
        """Subtraction operator."""
        return Vector(self.x - other.x, self.y - other.y)
    
    def __mul__(self, scalar):
        """Multiplication by scalar."""
        return Vector(self.x * scalar, self.y * scalar)
    
    def __len__(self):
        """len() function."""
        return int((self.x**2 + self.y**2)**0.5)
    
    def __getitem__(self, index):
        """Indexing (v[0] returns x)."""
        if index == 0:
            return self.x
        elif index == 1:
            return self.y
        else:
            raise IndexError("Vector index out of range")

v1 = Vector(1, 2)
v2 = Vector(3, 4)
v3 = v1 + v2                   # Vector(4, 6)
v4 = v1 * 2                    # Vector(2, 4)
len(v1)                        # ~2.236 (magnitude)
v1[0]                          # 1


"""
=============================================================================
7. FILE I/O & CONTEXT MANAGERS
=============================================================================
"""

# ---- WRITING TO FILE ----
with open('example.txt', 'w') as f:     # 'w' = write mode
    f.write("Hello, World!\n")
    f.write("Line 2\n")

# ---- READING FROM FILE ----
with open('example.txt', 'r') as f:     # 'r' = read mode
    content = f.read()                  # read entire file as string
    print(content)

with open('example.txt', 'r') as f:
    lines = f.readlines()               # read as list of lines
    # lines = ['Hello, World!\n', 'Line 2\n']

with open('example.txt', 'r') as f:
    for line in f:                      # iterate over lines
        print(line.strip())             # strip removes newline

# ---- APPEND MODE ----
with open('example.txt', 'a') as f:     # 'a' = append mode
    f.write("Line 3\n")

# ---- FILE MODES ----
# 'r'   = read (default)
# 'w'   = write (overwrites)
# 'a'   = append
# 'x'   = exclusive creation (fails if exists)
# 'b'   = binary (e.g., 'rb', 'wb')
# '+'   = read and write (e.g., 'r+', 'w+')

# ---- CONTEXT MANAGERS (with statement) ----
# with statement ensures resource cleanup (file closure) even if error occurs
# This is why: with open(...) is better than: f = open(...); f.close()

# Custom context manager
class MyContext:
    def __enter__(self):
        print("Entering context")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        print("Exiting context")
        return False  # don't suppress exceptions

with MyContext() as ctx:
    print("Inside context")

# Entering context
# Inside context
# Exiting context


"""
=============================================================================
8. COMPREHENSIONS & GENERATORS
=============================================================================
"""

# ---- LIST COMPREHENSIONS ----
numbers = [1, 2, 3, 4, 5]

# Basic syntax: [expression for item in iterable]
squared = [x**2 for x in numbers]       # [1, 4, 9, 16, 25]

# With condition: [expression for item in iterable if condition]
evens = [x for x in numbers if x % 2 == 0]  # [2, 4]

# Nested comprehension
matrix = [[i*j for j in range(1, 4)] for i in range(1, 4)]
# [[1, 2, 3], [2, 4, 6], [3, 6, 9]]

# ---- DICT COMPREHENSIONS ----
squares_dict = {x: x**2 for x in numbers}
# {1: 1, 2: 4, 3: 9, 4: 16, 5: 25}

keys = ['a', 'b', 'c']
values = [1, 2, 3]
my_dict = {k: v for k, v in zip(keys, values)}
# {'a': 1, 'b': 2, 'c': 3}

# With condition
even_squares = {x: x**2 for x in numbers if x % 2 == 0}
# {2: 4, 4: 16}

# ---- SET COMPREHENSIONS ----
unique_squares = {x**2 for x in [1, 2, 2, 3, 3, 3]}
# {1, 4, 9}

# ---- GENERATORS (memory efficient for large datasets) ----
def my_generator():
    """A generator that yields values one at a time."""
    yield 1
    yield 2
    yield 3

gen = my_generator()
next(gen)                       # 1
next(gen)                       # 2
next(gen)                       # 3
# next(gen)                     # StopIteration (exhausted)

# Generator expression (like list comp but lazy evaluation)
gen_squares = (x**2 for x in range(1000000))
next(gen_squares)               # 0 (doesn't create all 1M squares upfront)

# Iterate through generator
for value in my_generator():
    print(value)

# Generator function example (Fibonacci)
def fibonacci(n):
    """Generate first n Fibonacci numbers."""
    a, b = 0, 1
    for _ in range(n):
        yield a
        a, b = b, a + b

list(fibonacci(5))              # [0, 1, 1, 2, 3]


"""
=============================================================================
9. ERROR HANDLING
=============================================================================
"""

# ---- TRY / EXCEPT / ELSE / FINALLY ----
try:
    x = 10 / 2
except ZeroDivisionError:
    print("Cannot divide by zero")
else:
    print("Division successful:", x)  # executes if no exception
finally:
    print("Cleanup code")             # always executes

# Catching multiple exceptions
try:
    data = {'name': 'Jared'}
    value = int(data['age'])  # KeyError + ValueError possible
except KeyError:
    print("Key not found")
except ValueError:
    print("Cannot convert to int")
except Exception as e:              # catch all exceptions
    print(f"Unexpected error: {e}")

# Accessing exception information
try:
    x = 1 / 0
except ZeroDivisionError as e:
    print(f"Error type: {type(e)}")
    print(f"Error message: {e}")
    import traceback
    traceback.print_exc()             # print full stack trace

# ---- RAISING EXCEPTIONS ----
def validate_age(age):
    if age < 0:
        raise ValueError("Age cannot be negative")
    if age > 150:
        raise ValueError("Age cannot exceed 150")
    return True

try:
    validate_age(-5)
except ValueError as e:
    print(f"Validation error: {e}")

# Custom exceptions
class CustomError(Exception):
    pass

raise CustomError("Something went wrong")

# ---- COMMON EXCEPTIONS ----
# ValueError    - invalid value
# TypeError     - invalid type
# KeyError      - key not found in dict
# IndexError    - index out of range
# AttributeError - attribute not found
# NameError     - variable not defined
# ZeroDivisionError - division by zero
# FileNotFoundError - file not found
# ImportError   - module not found
# Exception     - base class for all exceptions


"""
=============================================================================
10. COMMON ALGORITHMS & PATTERNS
=============================================================================
"""

# ---- SORTING ----
numbers = [3, 1, 4, 1, 5, 9, 2, 6]

sorted_nums = sorted(numbers)           # [1, 1, 2, 3, 4, 5, 6, 9]
sorted_reverse = sorted(numbers, reverse=True)  # [9, 6, 5, 4, 3, 2, 1, 1]

# Sort by key function
words = ['apple', 'pie', 'a', 'longer']
by_length = sorted(words, key=len)      # ['a', 'pie', 'apple', 'longer']

people = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
by_age = sorted(people, key=lambda x: x['age'])  # sorted by age

# ---- SEARCHING ----
numbers = [1, 2, 3, 4, 5]

if 3 in numbers:                        # O(n) for list
    print("Found")

# Binary search (for sorted list, O(log n))
from bisect import bisect_left, bisect_right
index = bisect_left(numbers, 3)         # index where 3 is or would be

# ---- FREQUENCY COUNTING ----
from collections import Counter
words = ['apple', 'banana', 'apple', 'cherry', 'apple']
freq = Counter(words)
# Counter({'apple': 3, 'banana': 1, 'cherry': 1})
freq.most_common(2)                     # [('apple', 3), ('banana', 1)]

# ---- TWO POINTERS PATTERN ----
def is_palindrome(s):
    """Check if string is palindrome using two pointers."""
    left, right = 0, len(s) - 1
    while left < right:
        if s[left] != s[right]:
            return False
        left += 1
        right -= 1
    return True

# ---- SLIDING WINDOW ----
def max_sum_subarray(arr, k):
    """Find max sum of k consecutive elements."""
    if len(arr) < k:
        return None
    
    # Initial window
    window_sum = sum(arr[:k])
    max_sum = window_sum
    
    # Slide the window
    for i in range(k, len(arr)):
        window_sum = window_sum - arr[i-k] + arr[i]
        max_sum = max(max_sum, window_sum)
    
    return max_sum

max_sum_subarray([1, 4, 2, 10, 2, 3, 1, 0, 20], 4)  # 24 (10+2+3+1)

# ---- RECURSION ----
def factorial(n):
    """Calculate n! using recursion."""
    if n <= 1:
        return 1
    return n * factorial(n - 1)

factorial(5)                            # 120

# Tail recursion (can be optimized)
def factorial_tail(n, acc=1):
    """Tail-recursive factorial."""
    if n <= 1:
        return acc
    return factorial_tail(n - 1, acc * n)

# ---- BINARY SEARCH ----
def binary_search(arr, target):
    """Binary search on sorted array."""
    left, right = 0, len(arr) - 1
    
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    
    return -1

binary_search([1, 3, 5, 7, 9, 11], 7)  # 3


"""
=============================================================================
11. FUNCTIONAL PROGRAMMING
=============================================================================
"""

# ---- MAP ----
numbers = [1, 2, 3, 4, 5]

# map(function, iterable) -> returns iterator
squared = map(lambda x: x**2, numbers)
list(squared)                           # [1, 4, 9, 16, 25]

# ---- FILTER ----
# filter(function, iterable) -> returns iterator
evens = filter(lambda x: x % 2 == 0, numbers)
list(evens)                             # [2, 4]

# ---- REDUCE ----
from functools import reduce

# reduce(function, iterable) -> applies function cumulatively
product = reduce(lambda x, y: x * y, numbers)  # 120 (5! = 1*2*3*4*5)
sum_all = reduce(lambda x, y: x + y, numbers)  # 15

# ---- SORTED WITH CUSTOM KEY ----
words = ['apple', 'pie', 'a']
# Sort by length, then alphabetically
sorted_words = sorted(words, key=lambda x: (len(x), x))

# ---- ANY / ALL ----
numbers = [1, 2, 3, 4, 5]

any(x > 4 for x in numbers)             # True (at least one > 4)
all(x > 0 for x in numbers)             # True (all positive)
all(x > 3 for x in numbers)             # False (not all > 3)


"""
=============================================================================
12. REGULAR EXPRESSIONS
=============================================================================
"""

import re

text = "My email is jared.goroski@gmail.com and john@example.org"

# ---- PATTERN MATCHING ----
if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text):
    print("Email found")

# ---- FINDING ALL MATCHES ----
emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
# ['jared.goroski@gmail.com', 'john@example.org']

# ---- SUBSTITUTION ----
masked = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', 
                '[EMAIL]', text)
# "My email is [EMAIL] and [EMAIL]"

# ---- SPLITTING ----
text = "apple,banana;cherry:date"
parts = re.split(r'[,;:]', text)        # ['apple', 'banana', 'cherry', 'date']

# ---- COMMON PATTERNS ----
# \d        digit [0-9]
# \D        non-digit
# \w        word char [a-zA-Z0-9_]
# \W        non-word char
# \s        whitespace
# \S        non-whitespace
# \b        word boundary
# [abc]     any of a, b, c
# [a-z]     range from a to z
# [^abc]    not a, b, or c
# .         any character except newline
# *         zero or more
# +         one or more
# ?         zero or one
# {n}       exactly n
# {n,}      n or more
# {n,m}     between n and m

# ---- COMPILING PATTERNS (for reuse) ----
email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
matches = email_pattern.findall(text)
matches = email_pattern.sub('[EMAIL]', text)


"""
=============================================================================
13. DATETIME HANDLING
=============================================================================
"""

from datetime import datetime, date, timedelta
import time

# ---- CREATING DATES & TIMES ----
today = date.today()                    # 2024-07-15
now = datetime.now()                    # 2024-07-15 14:30:45.123456
specific = datetime(2024, 7, 15, 14, 30, 45)

# ---- FORMATTING ----
formatted = now.strftime("%Y-%m-%d %H:%M:%S")  # "2024-07-15 14:30:45"
formatted = now.strftime("%B %d, %Y")   # "July 15, 2024"

# ---- PARSING ----
date_str = "2024-07-15"
parsed = datetime.strptime(date_str, "%Y-%m-%d")  # datetime object

# ---- DATE ARITHMETIC ----
tomorrow = today + timedelta(days=1)
next_week = today + timedelta(weeks=1)
in_2_hours = now + timedelta(hours=2)
in_30_mins = now + timedelta(minutes=30)

# ---- DATE DIFFERENCE ----
date1 = datetime(2024, 1, 1)
date2 = datetime(2024, 7, 15)
difference = date2 - date1                    # timedelta(196 days)
days_passed = difference.days                 # 196

# ---- TIMEZONE AWARE DATES ----
from datetime import timezone
aware = datetime.now(timezone.utc)            # UTC time
aware = datetime(2024, 7, 15, 14, 30, 45, tzinfo=timezone.utc)

# ---- TIME ZONE CONVERSION ----
from pytz import timezone as tz
utc_time = datetime.now(tz('UTC'))
eastern = utc_time.astimezone(tz('US/Eastern'))


"""
=============================================================================
14. USEFUL BUILT-INS & TIPS
=============================================================================
"""

# ---- USEFUL BUILT-IN FUNCTIONS ----
len([1, 2, 3])                          # 3 (length)
sum([1, 2, 3, 4])                       # 10 (sum)
max([1, 5, 3])                          # 5 (maximum)
min([1, 5, 3])                          # 1 (minimum)
abs(-5)                                 # 5 (absolute value)
round(3.14159, 2)                       # 3.14 (round to 2 decimals)
pow(2, 3)                               # 8 (2^3)
divmod(17, 5)                           # (3, 2) (quotient, remainder)

# ---- TYPE & INSTANCE CHECKING ----
type(5)                                 # <class 'int'>
type([])                                # <class 'list'>
isinstance(5, int)                      # True
isinstance([1, 2], (list, tuple))       # True (check multiple types)

# ---- ZIP & UNZIP ----
list1 = [1, 2, 3]
list2 = ['a', 'b', 'c']
zipped = list(zip(list1, list2))        # [(1, 'a'), (2, 'b'), (3, 'c')]

# Unzip
unzipped = list(zip(*zipped))           # ([1, 2, 3], ('a', 'b', 'c'))

# ---- ENUMERATE ----
for index, value in enumerate(['a', 'b', 'c'], start=1):
    print(f"{index}: {value}")          # 1: a, 2: b, 3: c

# ---- REVERSED & SORTED ----
reversed_list = list(reversed([1, 2, 3, 4]))  # [4, 3, 2, 1]
sorted_list = sorted([3, 1, 4, 1, 5], reverse=True)  # [5, 4, 3, 1, 1]

# ---- EVAL & EXEC (USE WITH CAUTION!) ----
# eval() evaluates an expression, returns result
result = eval("2 + 2")                  # 4
result = eval("'hello'.upper()")        # 'HELLO'

# exec() executes code (statements)
code = """
x = 10
y = 20
z = x + y
"""
exec(code)
# WARNING: eval() and exec() can be security risks with untrusted input!

# ---- GLOBALS & LOCALS ----
def my_function():
    local_var = 10
    print(locals())                     # {'local_var': 10}

globals()                               # dictionary of global variables

# ---- GETATTR, SETATTR, HASATTR ----
class Person:
    name = "Jared"

person = Person()
getattr(person, 'name')                 # 'Jared'
getattr(person, 'age', 'Unknown')       # 'Unknown' (default if not found)

setattr(person, 'age', 25)              # person.age = 25
hasattr(person, 'name')                 # True
hasattr(person, 'age')                  # True

# ---- USEFUL MODULES ----
import math
math.sqrt(16)                           # 4.0
math.ceil(3.2)                          # 4
math.floor(3.8)                         # 3
math.factorial(5)                       # 120

import random
random.randint(1, 10)                   # random int between 1-10
random.choice([1, 2, 3, 4, 5])          # random choice from list
random.shuffle([1, 2, 3])               # shuffle in-place

import itertools
list(itertools.combinations([1, 2, 3], 2))  # [(1, 2), (1, 3), (2, 3)]
list(itertools.permutations([1, 2, 3], 2))  # [(1, 2), (1, 3), (2, 1), ...]

# ---- LIST / DICT / SET OPERATIONS ----
# Deep vs shallow copy
import copy
original = [[1, 2], [3, 4]]
shallow = copy.copy(original)           # references same inner lists
deep = copy.deepcopy(original)          # independent copy

# ---- WALRUS OPERATOR (:=) ----
# Python 3.8+: assign and use in same expression
if (n := len([1, 2, 3, 4, 5])) > 4:
    print(f"List has {n} elements")

# ---- F-STRING FORMATTING POWER ----
value = 3.14159
f"{value:.2f}"                          # '3.14' (2 decimal places)
f"{value:10.2f}"                        # '      3.14' (width 10)
f"{value:<10.2f}"                       # '3.14      ' (left align)
f"{value:>10.2f}"                       # '      3.14' (right align)

name = "Jared"
f"{name.upper()}"                       # 'JARED'
f"{len(name)}"                          # '5'

# ---- TIMING CODE ----
import time
start = time.time()
# ... code to time ...
end = time.time()
elapsed = end - start
print(f"Elapsed: {elapsed:.4f} seconds")

# Better: use timeit for small code snippets
import timeit
time_taken = timeit.timeit(lambda: sum(range(100)), number=10000)


"""
=============================================================================
END OF PYTHON FUNDAMENTALS REFERENCE
=============================================================================

KEY TAKEAWAYS:
1. Lists are mutable and ordered; use when you need to modify
2. Tuples are immutable; use for hashable, fixed data
3. Dicts are fast for lookups by key; O(1) average case
4. Sets are fast for membership testing; no duplicates
5. Use comprehensions for clean, Pythonic code
6. Generators are memory-efficient for large datasets
7. Context managers (with statement) handle cleanup automatically
8. List slicing with [start:end:step] is powerful and versatile
9. Decorators modify function behavior without changing the function
10. Exception handling with try/except is better than checking conditions
11. Lambda functions are good for simple, one-off operations
12. Use built-in functions (map, filter, sorted) instead of loops
13. Always use context managers for file I/O (with open(...))
14. f-strings are the modern, preferred way to format strings
15. Write defensive code: validate inputs, handle exceptions gracefully

PERFORMANCE NOTES:
- List lookup: O(n) average
- Dict lookup: O(1) average
- Set membership: O(1) average
- Sorting: O(n log n)
- Binary search: O(log n) on sorted data
- Generator vs list: generator is memory-efficient for large data

PYTHONIC PRINCIPLES (PEP 8):
- Use lowercase with underscores for variables: my_variable
- Use UPPERCASE for constants: MAX_SIZE
- Use CamelCase for classes: MyClass
- Keep functions small and focused (single responsibility)
- Write docstrings for functions and classes
- Use type hints for clarity (though optional): def greet(name: str) -> str:
- Use 4 spaces for indentation (not tabs)
- Keep lines under 79 characters
- Import order: standard library, third-party, local imports

"""
