import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, text
from sqlalchemy.orm import relationship

from sqlalchemy.types import TIMESTAMP

from . import Base

class Thread(Base):
	__tablename__ = 'threads'
	__table_args__ = {
		'mysql_engine': 'InnoDB',
		'mysql_charset': 'utf8'
	}

	id = Column(Integer, primary_key=True)
	title = Column(String(1000))

class ThreadVisit(Base):
	__tablename__ = 'threads_visits'
	__table_args__ = {
		'mysql_engine': 'InnoDB',
		'mysql_charset': 'utf8'
	}

	thread = Column(Integer, ForeignKey('threads.id'), primary_key=True)
	user = Column(Integer, ForeignKey('users.id'), primary_key=True)
	last_visit = Column(TIMESTAMP, nullable=False, default=datetime.datetime.utcnow, server_default=text('CURRENT_TIMESTAMP'))
	last_last_visit = Column(TIMESTAMP, nullable=True)
