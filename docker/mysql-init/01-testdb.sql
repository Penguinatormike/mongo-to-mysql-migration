-- A separate database for the integration tests, so they never touch the demo data.
CREATE DATABASE IF NOT EXISTS testdb;
GRANT ALL PRIVILEGES ON testdb.* TO 'app'@'%';
FLUSH PRIVILEGES;
