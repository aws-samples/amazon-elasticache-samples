CREATE database tutorial;
USE tutorial;
CREATE TABLE planet (
  id INT UNSIGNED AUTO_INCREMENT,
  name VARCHAR(30),
  PRIMARY KEY(id));
INSERT INTO planet (name) VALUES ("Mercury");
INSERT INTO planet (name) VALUES ("Venus");
INSERT INTO planet (name) VALUES ("Earth");
INSERT INTO planet (name) VALUES ("Mars");
INSERT INTO planet (name) VALUES ("Jupiter");
INSERT INTO planet (name) VALUES ("Saturn");
INSERT INTO planet (name) VALUES ("Uranus");
INSERT INTO planet (name) VALUES ("Neptune");
